"""
main.py — BTC futures cross-exchange spread monitor (Delta India vs Binance).
============================================================================

READ-ONLY. Public market data only — no API key, no order code anywhere.

    .venv\\Scripts\\python.exe crypto\\btc_arbitrage\\main.py

One asyncio process, independent tasks:

    delta socket      l1_orderbook + funding_rate  -> MarketData.delta
    binance socket    <symbol>@bookTicker           -> MarketData.binance
    evaluate          on EVERY quote update: spread engine -> detector -> 1 s bucket
    publisher         every TICK_WRITE_MS: data/state/TICK.json for the dashboard
                      (also re-evaluates, so an opportunity ENDS when data goes
                      stale even if no new quote arrives)
    second flush      every 1 s: one row into data/spread_db/{IST date}.sqlite
    funding / clock   Binance funding + USDC/USDT every minute, clock offset
                      against Binance server time every 5 minutes

The dashboard tab (deployment/btc_arb_api.py) only READS what this process
writes, so neither can stall the other. Lock port 47664 stops a duplicate.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import csv
import json
import time
import socket
import asyncio
import logging
import datetime as dt
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from core.market_data import MarketData
from core.fees import FeeModel
from core.spread_engine import compute, quality, threshold_usd, size_ok, LABEL
from core.opportunity_detector import OpportunityDetector
from core.paper import PaperTrader, depth_spreads
from storage.database import SpreadStore
from exchanges import delta, binance

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CSV_COLS = ["timestamp_ist", "event", "direction", "delta_bid", "delta_ask", "binance_bid",
            "binance_ask", "gross_spread", "fees", "net_spread", "spread_pct", "net_pct",
            "threshold", "delta_age_ms", "binance_age_ms", "duration_ms", "peak_net", "reason"]


def ist(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def setup_logging() -> logging.Logger:
    for d in (cfg.LOGS, cfg.STATE, cfg.DB_DIR):
        d.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("btcarb")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fmt.converter = lambda *a: dt.datetime.now(IST).timetuple()
    fh = RotatingFileHandler(cfg.APP_LOG, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def acquire_lock(port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def write_json_atomic(path: Path, obj, tries: int = 3):
    """Temp file + rename. Windows refuses the rename while another process (the
    dashboard reading it, Drive sync, antivirus) has the target open — retry
    briefly, then write in place (same approach as delta_btc/vwap/book.py)."""
    text = json.dumps(obj, separators=(",", ":"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (i + 1))
    path.write_text(text, encoding="utf-8")


class Monitor:
    def __init__(self, log):
        self.log = log
        self.fees = FeeModel.from_config(cfg)
        self.md = MarketData(on_update=self.on_quote)
        self.det = OpportunityDetector(cfg.OPPORTUNITY_MIN_MS, cfg.OPPORTUNITY_CHANGE_USD)
        self.store = SpreadStore(cfg.DB_DIR, cfg.RETENTION_DAYS)
        self.bcontract = binance.contract(cfg)
        self.last = None            # latest evaluation dict
        self.bucket = None          # the 1-second aggregate being built
        self.recent = deque(maxlen=240)     # ~60 s of 250 ms samples for the live line
        cfg.PAPER_DIR.mkdir(parents=True, exist_ok=True)
        self.paper = PaperTrader(cfg.PAPER_SIZE_BTC, self.fees, cfg.PAPER_CONTROL, cfg.PAPER_STATE,
                                 cfg.PAPER_TRADES, on_event=self.record_event, log=log)
        self.started = time.time()
        self.conn_state = {}
        self.events_logged = 0

    # ── evaluation ───────────────────────────────────────────────────────
    def _binance_factor(self) -> float:
        if cfg.ADJUST_QUOTE_TO_USD and self.bcontract["quote"] == "USDT" and self.md.usdc_usdt:
            return 1 / self.md.usdc_usdt          # USDT price -> USD (USDC ~ USD)
        return 1.0

    def evaluate(self):
        dq, bq = self.md.delta.quote, self.md.binance.quote
        if not dq or not bq:
            return None
        now_mono, now_ms = time.monotonic(), time.time() * 1000
        k = self._binance_factor()
        s = compute(dq.bid, dq.ask, bq.bid * k, bq.ask * k, self.fees)
        d_age, b_age = dq.age_ms(now_mono), bq.age_ms(now_mono)
        q = quality(d_age, b_age, self.md.delta.connected, self.md.binance.connected,
                    cfg.MAX_DATA_AGE_MS, cfg.MAX_QUOTE_TIME_DIFF_MS)
        thr = threshold_usd(s.d_mid, cfg.OPPORTUNITY_THRESHOLD_USD, cfg.OPPORTUNITY_THRESHOLD_PCT)
        for e in self.det.update(s, q, thr, now_ms):
            e.update(delta_age_ms=round(d_age), binance_age_ms=round(b_age))
            self.record_event(e)
        # depth-priced (PAPER_SIZE_BTC) spreads -> the paper trader
        dd, bd = self.md.depth["delta"], self.md.depth["binance"]
        dd_age = dd.age_ms(now_mono) if dd else None
        bd_age = bd.age_ms(now_mono) if bd else None
        depth_ok = (dd_age is not None and bd_age is not None
                    and dd_age <= cfg.MAX_DATA_AGE_MS and bd_age <= cfg.MAX_DATA_AGE_MS)
        sp = depth_spreads(dd, bd, cfg.PAPER_SIZE_BTC) if depth_ok else None
        if sp and k != 1.0:
            for key in ("b_bid", "b_ask"):
                sp[key] *= k
            sp["s1"], sp["s2"] = sp["b_bid"] - sp["d_ask"], sp["d_bid"] - sp["b_ask"]
        self.paper.step(sp, q == "OK" and sp is not None, now_mono)
        ev = {"s": s, "quality": q, "threshold": thr, "d_age": d_age, "b_age": b_age,
              "sp": sp, "dd_age": dd_age, "bd_age": bd_age,
              "now_ms": now_ms, "opportunity": self.det.active,
              "size_ok": size_ok(s.direction, dq.bid_qty, dq.ask_qty, bq.bid_qty, bq.ask_qty,
                                 cfg.TRADE_SIZE_BTC)}
        self.last = ev
        self._fold(ev)
        return ev

    def on_quote(self, _name):
        try:
            self.evaluate()
        except Exception as e:
            self.log.exception(f"evaluate failed: {e}")

    # ── 1-second bucket ─────────────────────────────────────────────────
    def _fold(self, ev):
        s, t = ev["s"], int(ev["now_ms"] // 1000)
        b = self.bucket
        if b is None or b["t"] != t:
            if b is not None:
                self._flush(b)
            b = self.bucket = {"t": t, "n": 0, "s1_max": s.s1, "s2_max": s.s2,
                               "net1_max": s.net1, "net2_max": s.net2,
                               "mid_min": s.mid_spread, "mid_max": s.mid_spread, "opp": False,
                               "s1_1btc_max": None, "s2_1btc_max": None}
        b["n"] += 1
        b["s1_max"] = max(b["s1_max"], s.s1)
        b["s2_max"] = max(b["s2_max"], s.s2)
        b["net1_max"] = max(b["net1_max"], s.net1)
        b["net2_max"] = max(b["net2_max"], s.net2)
        b["mid_min"] = min(b["mid_min"], s.mid_spread)
        b["mid_max"] = max(b["mid_max"], s.mid_spread)
        b["opp"] = b["opp"] or ev["opportunity"]
        sp = ev.get("sp")
        if sp:
            for key, v in (("s1_1btc_max", sp["s1"]), ("s2_1btc_max", sp["s2"])):
                b[key] = v if b[key] is None else max(b[key], v)
        b["last"] = ev

    def _flush(self, b):
        ev = b.get("last")
        if not ev:
            return
        s, dq, bq = ev["s"], self.md.delta.quote, self.md.binance.quote
        row = {"t": b["t"], "n": b["n"],
               "d_bid": s.d_bid, "d_ask": s.d_ask, "b_bid": s.b_bid, "b_ask": s.b_ask,
               "d_bid_qty": dq.bid_qty if dq else None, "d_ask_qty": dq.ask_qty if dq else None,
               "b_bid_qty": bq.bid_qty if bq else None, "b_ask_qty": bq.ask_qty if bq else None,
               "d_mid": s.d_mid, "b_mid": s.b_mid,
               "mid_spread": s.mid_spread, "mid_spread_pct": s.mid_spread_pct,
               "mid_min": b["mid_min"], "mid_max": b["mid_max"],
               "s1": s.s1, "s2": s.s2, "s1_max": b["s1_max"], "s2_max": b["s2_max"],
               "net1_max": b["net1_max"], "net2_max": b["net2_max"],
               "direction": s.direction, "gross_spread": s.gross, "fees": s.fees,
               "net_spread": s.net, "spread_pct": s.gross_pct,
               "edge": s.edge, "edge_net": s.edge_net,
               "quality": ev["quality"], "opportunity": int(b["opp"]),
               "d_age_ms": round(ev["d_age"]), "b_age_ms": round(ev["b_age"]),
               "d_latency_ms": self.md.latency_ms("delta"),
               "b_latency_ms": self.md.latency_ms("binance"),
               "usdc_usdt": self.md.usdc_usdt,
               "s1_1btc": ev["sp"]["s1"] if ev.get("sp") else None,
               "s2_1btc": ev["sp"]["s2"] if ev.get("sp") else None,
               "s1_1btc_max": b["s1_1btc_max"], "s2_1btc_max": b["s2_1btc_max"],
               "d_depth_age_ms": round(ev["dd_age"]) if ev.get("dd_age") is not None else None,
               "b_depth_age_ms": round(ev["bd_age"]) if ev.get("bd_age") is not None else None}
        try:
            self.store.write(row)
        except Exception as e:
            self.log.error(f"sqlite write failed: {type(e).__name__}: {e}")

    # ── events ───────────────────────────────────────────────────────────
    def record_event(self, e: dict):
        e = dict(e)
        e.setdefault("ts_ms", time.time() * 1000)
        e["ts"] = ist(e["ts_ms"])
        e["label"] = LABEL.get(e.get("direction"), "")
        try:
            with open(cfg.EVENTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e) + "\n")
        except OSError:
            pass
        if e["event"] in ("STARTED", "CHANGED", "ENDED"):
            self.log.info(f"OPPORTUNITY {e['event']} {e['label']} gross ${e['gross']:.2f} "
                          f"fees ${e['fees']:.2f} net ${e['net']:.2f} ({e['spread_pct']:.4f}%)"
                          + (f" — {e.get('reason')}" if e.get("reason") else ""))
            new = not cfg.OPPORTUNITIES_CSV.exists()
            try:
                with open(cfg.OPPORTUNITIES_CSV, "a", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    if new:
                        w.writerow(CSV_COLS)
                    w.writerow([e["ts"], e["event"], e["direction"], e["d_bid"], e["d_ask"],
                                e["b_bid"], e["b_ask"], e["gross"], e["fees"], e["net"],
                                e["spread_pct"], e["net_pct"], e["threshold"],
                                e.get("delta_age_ms"), e.get("binance_age_ms"),
                                e.get("duration_ms"), e.get("peak_net"), e.get("reason", "")])
            except OSError as ex:
                self.log.error(f"opportunities.csv write failed: {ex}")
        elif e["event"].startswith("PAPER_"):
            self.log.info("PAPER " + e["event"][6:] + " " + " ".join(
                f"{k}={v}" for k, v in e.items() if k not in ("event", "ts", "ts_ms", "label", "direction")))
        self.events_logged += 1

    def watch_connections(self):
        for f in (self.md.delta, self.md.binance):
            prev = self.conn_state.get(f.name)
            if prev is not None and prev != f.connected:
                self.record_event({"event": "CONNECTED" if f.connected else "DISCONNECTED",
                                   "exchange": f.name, "error": "" if f.connected else f.last_error})
            self.conn_state[f.name] = f.connected

    # ── publishing ───────────────────────────────────────────────────────
    def tick(self) -> dict:
        now_mono = time.monotonic()
        ev = self.evaluate() if (self.md.delta.quote and self.md.binance.quote) else None
        out = {
            "ok": True, "ts_ms": time.time() * 1000, "ts": ist(time.time() * 1000),
            "uptime_s": round(time.time() - self.started),
            "config": {
                "delta_symbol": cfg.DELTA_SYMBOL, "binance_symbol": cfg.BINANCE_SYMBOL,
                "binance_market": self.bcontract["market"], "binance_quote": self.bcontract["quote"],
                "adjust_quote_to_usd": cfg.ADJUST_QUOTE_TO_USD,
                "delta_taker_fee": cfg.DELTA_TAKER_FEE, "delta_fee_gst": cfg.DELTA_FEE_GST,
                "binance_taker_fee": cfg.BINANCE_TAKER_FEE, "fee_mode": cfg.FEE_MODE,
                "fee_cost_pct": round(self.fees.cost_pct(), 5),
                "threshold_usd": cfg.OPPORTUNITY_THRESHOLD_USD,
                "threshold_pct": cfg.OPPORTUNITY_THRESHOLD_PCT,
                "min_ms": cfg.OPPORTUNITY_MIN_MS, "trade_size_btc": cfg.TRADE_SIZE_BTC,
                "max_data_age_ms": cfg.MAX_DATA_AGE_MS,
                "max_quote_time_diff_ms": cfg.MAX_QUOTE_TIME_DIFF_MS,
            },
            "clock_offset_ms": round(self.md.clock_offset_ms, 1),
            "usdc_usdt": self.md.usdc_usdt,
            "feeds": {},
            "paper": self.paper.snapshot(),
            "depth": {"delta_age_ms": round(dd.age_ms(now_mono)) if (dd := self.md.depth["delta"]) else None,
                      "binance_age_ms": round(bd.age_ms(now_mono)) if (bd := self.md.depth["binance"]) else None,
                      "delta_levels": len(dd.bids) + len(dd.asks) if dd else 0,
                      "binance_levels": len(bd.bids) + len(bd.asks) if bd else 0},
        }
        for f in (self.md.delta, self.md.binance):
            st = f.status(now_mono)
            q = f.quote
            st.update(bid=q.bid if q else None, ask=q.ask if q else None,
                      bid_qty=q.bid_qty if q else None, ask_qty=q.ask_qty if q else None,
                      exchange_ts_ms=q.exchange_ts_ms if q else None,
                      latency_ms=self.md.latency_ms(f.name))
            out["feeds"][f.name] = st
        if ev:
            s = ev["s"]
            out.update(spread={k: (round(v, 6) if isinstance(v, float) else v)
                               for k, v in s.as_dict().items()},
                       direction_label=LABEL[s.direction], quality=ev["quality"],
                       threshold=round(ev["threshold"], 2), opportunity=ev["opportunity"],
                       size_ok=ev["size_ok"])
            self.recent.append([round(ev["now_ms"] / 1000, 3), round(s.mid_spread, 3),
                                round(s.s1, 3), round(-s.s2, 3), round(s.edge_net, 3)])
            out["recent"] = list(self.recent)
        else:
            out.update(quality="NO_DATA", opportunity=False, recent=list(self.recent))
        return out

    # ── loops ────────────────────────────────────────────────────────────
    async def publisher(self, stop):
        while not stop.is_set():
            try:
                self.paper.refresh_control()
                self.watch_connections()
                write_json_atomic(cfg.TICK_FILE, self.tick())
            except Exception as e:
                self.log.error(f"publish failed: {type(e).__name__}: {e}")
            await self._sleep(stop, cfg.TICK_WRITE_MS / 1000)

    async def second_flush(self, stop):
        """Close the 1-s bucket even when quotes go quiet, so a gap shows as a gap."""
        while not stop.is_set():
            await self._sleep(stop, 1.0)
            b = self.bucket
            if b and b["t"] < int(time.time()) - 1:
                self._flush(b)
                self.bucket = None

    async def funding_loop(self, stop):
        while not stop.is_set():
            try:
                r, iv, nxt = await asyncio.to_thread(binance.funding, cfg)
                f = self.md.binance
                f.funding_rate, f.funding_interval_h, f.next_funding_ms = r, iv, nxt
            except Exception as e:
                self.log.warning(f"binance funding poll failed: {type(e).__name__}: {e}")
            try:
                self.md.usdc_usdt = await asyncio.to_thread(binance.usdc_usdt, cfg)
            except Exception as e:
                self.log.warning(f"USDC/USDT poll failed: {type(e).__name__}: {e}")
            await self._sleep(stop, cfg.FUNDING_POLL_S)

    async def clock_loop(self, stop):
        """NTP-style offset of OUR clock against Binance server time. Used only to
        turn exchange stamps into a latency figure; ages never depend on it."""
        while not stop.is_set():
            try:
                best = None
                for _ in range(3):
                    t0 = time.time() * 1000
                    srv = await asyncio.to_thread(binance.server_time_ms, cfg)
                    t1 = time.time() * 1000
                    if best is None or (t1 - t0) < best[0]:
                        best = (t1 - t0, srv - (t0 + t1) / 2)
                self.md.clock_offset_ms = best[1]
                self.log.info(f"clock: local offset {best[1]:+.0f} ms vs Binance (rtt {best[0]:.0f} ms)")
            except Exception as e:
                self.log.warning(f"clock sync failed: {type(e).__name__}: {e}")
            await self._sleep(stop, cfg.CLOCK_SYNC_S)

    @staticmethod
    async def _sleep(stop, s):
        try:
            await asyncio.wait_for(stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass


def verify(log) -> bool:
    """Contract checks before any socket opens. A MISMATCH is fatal (wrong
    instrument = meaningless spread); a network failure is retried."""
    while True:
        try:
            delta.verify_contract(cfg, log)
            binance.verify_contract(cfg, log)
            return True
        except RuntimeError as e:
            log.error(f"REFUSING TO START: {e}")
            write_json_atomic(cfg.TICK_FILE, {"ok": False, "ts_ms": time.time() * 1000,
                                              "fatal": str(e)})
            return False
        except Exception as e:
            log.warning(f"contract check could not reach the exchange ({type(e).__name__}: {e}) "
                        f"— retrying in 30 s")
            time.sleep(30)


async def amain(log):
    mon = Monitor(log)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(delta.run(cfg, mon.md, log, stop), name="delta"),
        asyncio.create_task(binance.run(cfg, mon.md, log, stop), name="binance"),
        asyncio.create_task(mon.publisher(stop), name="publisher"),
        asyncio.create_task(mon.second_flush(stop), name="flush"),
        asyncio.create_task(mon.funding_loop(stop), name="funding"),
        asyncio.create_task(mon.clock_loop(stop), name="clock"),
    ]
    try:
        # any task dying unexpectedly is a bug: log it and take the process down
        # so the scheduled task restarts it clean, rather than run half-blind
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                log.error(f"task {t.get_name()} crashed: {t.exception()!r}")
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        mon.store.close()


def main():
    log = setup_logging()
    lock = acquire_lock(cfg.LOCK_PORT)
    if lock is None:
        print(f"another BTC arbitrage monitor holds port {cfg.LOCK_PORT} — exiting", flush=True)
        return 0
    log.info(f"BTC arbitrage monitor starting — {cfg.DELTA_SYMBOL} (Delta) vs "
             f"{cfg.BINANCE_SYMBOL} (Binance) — READ-ONLY, public data")
    if not verify(log):
        return 2
    try:
        asyncio.run(amain(log))
    except KeyboardInterrupt:
        log.info("stopped by user")
    return 1


if __name__ == "__main__":
    sys.exit(main())
