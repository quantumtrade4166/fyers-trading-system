import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Taker Flow L/S — PAPER engine (Delta Exchange India prices + charges).

Long the coins with the most aggressive taker BUYING, short the most aggressive
SELLING (Binance 14-day taker-buy ratio), rebalance every 3 days at 00:05 UTC
(05:35 IST). $10,000 paper capital, 0.5x per book. Places NO real orders.

Every minute : Delta marks -> MTM; hourly funding accrual; publish TICK.json
Every 5 min  : one point on the cumulative P&L curve (equity.jsonl)
Rebalance    : signal -> target contracts -> taker fills walking Delta's L2 book
               (fee + 18% GST + real slippage) -> trades.jsonl, rebalances.jsonl

Files: live_trading_options/taker_flow/data/state/   (read by deployment/takerflow_api.py)

Run  : .venv\\Scripts\\python.exe -u -m live_trading_options.taker_flow.engine
       --dry-run   print today's signal + target book, trade nothing
       --rebalance-now   force a rebalance on start (paper)
"""

import argparse
import json
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .flow_signal import build_signal, delta_perps
from .paper_broker import Ledger, accrue_funding, execute, split_existing, target_contracts, utc_now

BASE = Path(__file__).parent
CFG = BASE / "config" / "parameters.json"
LOCK_PORT = 47678


class Engine:
    def __init__(self, state_dir: Path):
        self.cfg = json.loads(CFG.read_text(encoding="utf-8"))
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.dir / "ledger.json"
        self.ledger = Ledger.load(self.ledger_path, self.cfg["capital_usd"])
        self.delta: dict = {}
        self.last_signal: dict = self._read_json("signal.json") or {}
        self.last_sample = 0.0
        self.status = "starting"

    # ── io ──
    def _read_json(self, name):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _append(self, name: str, row: dict) -> None:
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def log(self, msg: str, kind: str = "info") -> None:
        row = {"ts": utc_now(), "kind": kind, "msg": msg}
        print(f"{row['ts']} [{kind}] {msg}", flush=True)
        self._append("events.jsonl", row)

    def _write(self, name: str, obj) -> None:
        p = self.dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        for _ in range(5):
            try:
                tmp.replace(p)
                return
            except PermissionError:
                time.sleep(0.2)
        p.write_text(json.dumps(obj), encoding="utf-8")

    # ── schedule ──
    def next_rebalance(self) -> datetime:
        hh, mm = map(int, self.cfg["rebalance_utc"].split(":"))
        if not self.ledger.last_rebalance:
            return datetime.now(timezone.utc)
        last = datetime.fromisoformat(self.ledger.last_rebalance).astimezone(timezone.utc)
        day = (last + timedelta(days=self.cfg["rebalance_every_days"])).date()
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)

    # ── market ──
    def refresh_marks(self) -> None:
        self.delta = delta_perps()
        for u, p in self.ledger.positions.items():
            if u in self.delta:
                p.mark = self.delta[u]["mark"]
            else:
                self.log(f"{p.symbol} no longer listed/operational on Delta — mark frozen", "warn")

    def funding_tick(self) -> None:
        now = datetime.now(timezone.utc)
        key = now.strftime("%Y-%m-%dT%H")
        if not self.ledger.last_funding_hour:
            self.ledger.last_funding_hour = key          # start counting from the next hour
            return
        last = datetime.strptime(self.ledger.last_funding_hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        hours = int((now.replace(minute=0, second=0, microsecond=0) - last).total_seconds() // 3600)
        if hours <= 0:
            return
        total = 0.0
        for _ in range(min(hours, 72)):                  # missed hours after a restart
            self.ledger.last_funding_hour = ""
            total += accrue_funding(self.ledger, self.delta, key)
        if hours > 1:
            self.log(f"funding: accrued {hours} missed hours at current rates", "warn")
        self.ledger.last_funding_hour = key
        self._append("funding.jsonl", {"ts": utc_now(), "hours": hours, "amount": round(total, 4)})

    # ── daily Delta snapshot + shadow backtest ──
    def snapshot_delta(self) -> None:
        """Once per UTC day: every live Delta perp's turnover, spread and depth —
        the shadow backtest's tradable set and cost model come from these."""
        d = self.dir / "delta_snapshots"
        d.mkdir(exist_ok=True)
        f = d / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".json")
        if not f.exists() and self.delta:
            f.write_text(json.dumps(self.delta), encoding="utf-8")

    def maybe_shadow(self) -> None:
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < 20:
            return                                       # let Binance finalise the daily bar
        if getattr(self, "_shadow_thread", None) and self._shadow_thread.is_alive():
            return
        sh = self._read_json("shadow.json") or {}
        if (sh.get("asof") or "")[:10] == now.strftime("%Y-%m-%d"):
            return
        if getattr(self, "_shadow_tried", None) == now.strftime("%Y-%m-%dT%H"):
            return                                       # failed this hour already
        self._shadow_tried = now.strftime("%Y-%m-%dT%H")

        def job():
            try:
                from .shadow_backtest import run as shadow_run
                shadow_run(self.dir, log=lambda m: self.log(m))
            except Exception as e:
                self.log(f"shadow backtest failed: {type(e).__name__}: {e}", "error")
        self._shadow_thread = threading.Thread(target=job, daemon=True, name="shadow")
        self._shadow_thread.start()

    # ── rebalance ──
    def rebalance(self, dry_run: bool = False) -> bool:
        self.status = "rebalancing"
        self.log("rebalance: building signal (Binance taker flow, Delta universe)")
        sig = build_signal(self.cfg, log=self.log)
        self.delta = sig.pop("delta")
        self.refresh_marks()
        eq = self.ledger.equity()
        lev = self.cfg["leverage_per_book"]
        targets = {u: target_contracts(eq, lev, w, self.delta[u]) for u, w in sig["weights"].items()}
        current = {u: p.contracts for u, p in self.ledger.positions.items()}
        orders = []
        for u in set(targets) | set(current):
            d = targets.get(u, 0) - current.get(u, 0)
            if d:
                closing = targets.get(u, 0) == 0 or (current.get(u, 0) and abs(targets.get(u, 0)) < abs(current.get(u, 0)))
                orders.append((0 if closing else 1, u, d))
        orders.sort()                                    # closes/reductions first, then opens
        self.log(f"rebalance: equity ${eq:,.2f}, {sig['eligible']} eligible coins, "
                 f"{len(sig['longs'])} long / {len(sig['shorts'])} short, {len(orders)} orders")
        if dry_run:
            for _, u, d in orders:
                info = self.delta[u]
                print(f"  {u:<10} {'BUY ' if d > 0 else 'SELL'} {abs(d):>7} contracts  "
                      f"(${abs(d) * info['contract_value'] * info['mark']:,.0f})  target {targets.get(u, 0)}")
            return True
        ok = True
        for _, u, d in orders:
            if u not in self.delta:
                self.log(f"cannot trade {u}: not live on Delta", "error")
                ok = False
                continue
            try:
                t = execute(self.ledger, self.delta[u], u, d, "rebalance")
                self.ledger.save(self.ledger_path)
                self._append("trades.jsonl", t)
                if t["thin_book"]:
                    self.log(f"{t['symbol']}: order larger than visible book — filled at last level", "warn")
            except Exception as e:
                self.log(f"order {u} {d:+d} failed: {type(e).__name__}: {e}", "error")
                ok = False
        sig["equity_before"] = eq
        sig["orders"] = len(orders)
        sig["complete"] = ok
        self._append("rebalances.jsonl", {k: v for k, v in sig.items() if k != "table"})
        self.last_signal = sig
        self._write("signal.json", sig)
        if ok:
            self.ledger.last_rebalance = utc_now()
            self.ledger.save(self.ledger_path)
            self.log(f"rebalance done: {len(self.ledger.positions)} positions, "
                     f"gross ${self.ledger.gross_exposure():,.0f}")
        else:
            self.log("rebalance INCOMPLETE — will retry in 5 minutes", "error")
        self.sample(force=True)
        return ok

    # ── publish ──
    def sample(self, force: bool = False) -> None:
        if not force and time.time() - self.last_sample < self.cfg["equity_sample_every_s"]:
            return
        L = self.ledger
        self._append("equity.jsonl", {
            "ts": utc_now(), "equity": round(L.equity(), 2),
            "pnl": round(L.equity() - L.capital, 2), "realized": round(L.realized, 2),
            "unrealized": round(L.unrealized(), 2), "fees": round(L.fees, 2), "gst": round(L.gst, 2),
            "funding": round(L.funding, 2), "gross": round(L.gross_exposure(), 2)})
        self.last_sample = time.time()

    def publish(self) -> None:
        L = self.ledger
        w = (self.last_signal or {}).get("weights", {})
        ratios = {r["delta"]: r["taker_ratio"] for r in (self.last_signal or {}).get("table", []) if r.get("delta")}
        pos = []
        for u, p in sorted(L.positions.items(), key=lambda kv: -kv[1].contracts * kv[1].contract_value * kv[1].mark):
            notional = p.contracts * p.contract_value * p.mark
            info = self.delta.get(u, {})
            pos.append({"underlying": u, "symbol": p.symbol, "side": "LONG" if p.contracts > 0 else "SHORT",
                        "contracts": p.contracts, "contract_value": p.contract_value,
                        "avg_price": p.avg_price, "mark": p.mark, "notional": round(notional, 2),
                        "upnl": round((p.mark - p.avg_price) * p.contracts * p.contract_value, 2),
                        "weight": w.get(u), "taker_ratio": ratios.get(u),
                        "funding_8h_pct": info.get("funding_rate_8h_pct")})
        self._write("TICK.json", {
            "ts": utc_now(), "status": self.status, "mode": "paper", "strategy": self.cfg["strategy"],
            "capital": L.capital, "equity": round(L.equity(), 2), "pnl": round(L.equity() - L.capital, 2),
            "realized": round(L.realized, 2), "unrealized": round(L.unrealized(), 2),
            "fees": round(L.fees, 2), "gst": round(L.gst, 2), "slippage": round(L.slippage, 2),
            "funding": round(L.funding, 2), "gross": round(L.gross_exposure(), 2),
            "long_usd": round(sum(x["notional"] for x in pos if x["notional"] > 0), 2),
            "short_usd": round(-sum(x["notional"] for x in pos if x["notional"] < 0), 2),
            "long_book": L.book("long"), "short_book": L.book("short"),
            "positions": pos, "started": L.started, "last_rebalance": L.last_rebalance,
            "next_rebalance": self.next_rebalance().isoformat(timespec="seconds"),
            "params": {k: self.cfg[k] for k in ("leverage_per_book", "lookback_days", "rebalance_every_days",
                                                "bucket_q", "universe_top_n", "min_delta_turnover_usd",
                                                "taker_fee", "gst")}})

    # ── loop ──
    def run(self, rebalance_now: bool = False) -> None:
        self.log(f"engine start — paper, capital ${self.ledger.capital:,.0f}, "
                 f"{len(self.ledger.positions)} open positions")
        retry_at = 0.0
        if not (self.dir / "equity.jsonl").exists():
            self.sample(force=True)                      # the curve starts at $0, before any fee
        if rebalance_now:
            self.ledger.last_rebalance = ""
        while True:
            try:
                self.refresh_marks()
                if not getattr(self, "_split_checked", False):
                    self._split_checked = True
                    trades = [json.loads(l) for l in open(self.dir / "trades.jsonl", encoding="utf-8")]                         if (self.dir / "trades.jsonl").exists() else []
                    hours = sum(json.loads(l).get("hours", 0) for l in open(self.dir / "funding.jsonl", encoding="utf-8"))                         if (self.dir / "funding.jsonl").exists() else 0
                    if split_existing(self.ledger, trades, self.delta, hours):
                        self.ledger.save(self.ledger_path)
                        self.log(f"ledger: split {len(trades)} earlier fills and {hours} h of funding into long/short books")
                self.snapshot_delta()
                self.funding_tick()
                now = datetime.now(timezone.utc)
                if now >= self.next_rebalance() and time.time() >= retry_at:
                    if not self.rebalance():
                        retry_at = time.time() + 300
                self.status = "running"
                self.ledger.save(self.ledger_path)
                self.maybe_shadow()
                self.sample()
                self.publish()
            except Exception as e:
                self.status = "error"
                self.log(f"loop error: {type(e).__name__}: {e}\n{traceback.format_exc()}", "error")
                try:
                    self.publish()
                except Exception:
                    pass
            time.sleep(self.cfg["mtm_every_s"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebalance-now", action="store_true")
    ap.add_argument("--state-dir", default=str(BASE / "data" / "state"))
    args = ap.parse_args()
    eng = Engine(Path(args.state_dir))
    if args.dry_run:
        eng.rebalance(dry_run=True)
        return
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print(f"another Taker Flow engine holds port {LOCK_PORT} — exiting", flush=True)
        return
    eng.run(rebalance_now=args.rebalance_now)


if __name__ == "__main__":
    main()
