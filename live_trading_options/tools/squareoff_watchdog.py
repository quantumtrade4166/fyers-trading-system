"""
squareoff_watchdog.py — the last line of defence, outside both engines.
======================================================================

Switching both strangles to NRML removed a safety net: Zerodha auto-squares-off
MIS intraday positions around 15:20, and NRML positions it does NOT. So the
15:14 square-off inside each engine became the ONLY thing closing a position —
and this week alone produced an engine that failed to restart, a latched KILL
that silently disabled a session, and an exception that unwound a whole tick.
If any of those happen after 15:14, an NRML short carries overnight.

This runs as its OWN scheduled task at 15:20, in its OWN process, with its own
Kite session. It does not import either engine and does not care whether they
are alive. It asks one question:

    does this account still hold a SHORT that one of MY strategies opened?

and if so, buys it back and keeps trying until the broker confirms it is gone.

OWN-BOOK, always
----------------
Positions carry no tag — only orders do. So what we are short is reconstructed
from TODAY'S COMPLETE orders bearing our tags, exactly the way the engines'
restart-reconciliation does. The user's own manual trades on the same strikes
are invisible to it, and it will never buy back more than our own orders say we
are short, nor more than the broker actually shows.

    .venv\\Scripts\\python.exe live_trading_options/tools/squareoff_watchdog.py
    .venv\\Scripts\\python.exe live_trading_options/tools/squareoff_watchdog.py --dry-run
"""

import sys
import time
import argparse
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strangle_strategy" / "live"))

import kite_executor as kx                                   # noqa: E402
import audit                                                 # noqa: E402

TAGS = ("dnstrangle", "vwstrangle")
BUY, SELL = "BUY", "SELL"

# How hard we are willing to reach through the book, per round. This is the exit
# of last resort: not filling is worse than paying up.
CUSHIONS = (2, 6, 12, 25, 50)
ROUND_PAUSE = 4.0          # seconds between rounds
FILL_WAIT = 6.0            # seconds to wait for a fill before re-pricing


class _Tee:
    """Console + a log file that is ALWAYS UTF-8. Shell redirection is not safe on
    Windows: PowerShell 5.1 writes UTF-16, cmd writes UTF-8, and a file that gets
    both is unreadable from the first mixed byte onward."""

    def __init__(self, stream, fh):
        self._s, self._f = stream, fh

    def write(self, text):
        self._s.write(text)
        try:
            self._f.write(text)
            self._f.flush()
        except Exception:
            pass

    def flush(self):
        self._s.flush()


def _tee(path: str):
    f = Path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    fh = open(f, "a", encoding="utf-8", newline="\n")
    fh.write(f"\n{'=' * 70}\n")
    sys.stdout = _Tee(sys.stdout, fh)


def log(msg: str):
    print(f"  [squareoff] {dt.datetime.now():%H:%M:%S} {msg}", flush=True)


# ── what WE are short, from our own orders ────────────────────────────────
def own_net_shorts(kite) -> dict:
    """{tradingsymbol: {qty, exchange, tag}} for every symbol our tags are net
    SHORT in today. Only COMPLETE orders count — a rejected or pending order
    never moved anything."""
    net = {}
    try:
        orders = kite.orders() or []
    except Exception as e:
        log(f"could not read the order book: {e}")
        return {}
    for o in orders:
        tag = (o.get("tag") or "")
        if not any(tag.startswith(t) for t in TAGS):
            continue
        if o.get("status") != "COMPLETE":
            continue
        ts = o.get("tradingsymbol")
        qty = int(o.get("filled_quantity") or o.get("quantity") or 0)
        if not ts or qty <= 0:
            continue
        row = net.setdefault(ts, {"qty": 0, "exchange": o.get("exchange"), "tag": tag})
        row["qty"] += qty if o.get("transaction_type") == SELL else -qty
    return {ts: r for ts, r in net.items() if r["qty"] > 0}


def broker_shorts(kite) -> dict:
    """{tradingsymbol: {qty, product, exchange}} — what the account is REALLY
    short right now, and crucially in WHICH product. The buy-back must match it:
    a BUY in the wrong product does not close the short, it opens a long."""
    out = {}
    try:
        for p in (kite.positions() or {}).get("net", []):
            q = int(p.get("quantity") or 0)
            if q < 0:
                out[p["tradingsymbol"]] = {"qty": -q, "product": p.get("product"),
                                           "exchange": p.get("exchange")}
    except Exception as e:
        log(f"could not read positions: {e}")
    return out


def to_close(kite) -> list:
    """The intersection: shorts that are BOTH ours and actually open.

    Capped by the broker's real quantity, so a stale or double-counted order can
    never make us buy back more than exists."""
    mine, live = own_net_shorts(kite), broker_shorts(kite)
    jobs = []
    for ts, row in mine.items():
        b = live.get(ts)
        if not b:
            continue                                   # already flat — nothing to do
        qty = min(row["qty"], b["qty"])
        if qty > 0:
            jobs.append({"tradingsymbol": ts, "qty": qty,
                         "exchange": b.get("exchange") or row.get("exchange"),
                         "product": b.get("product"), "tag": row["tag"]})
    return jobs


# ── closing one ───────────────────────────────────────────────────────────
def close_one(kite, job, cushion: int, dry: bool) -> bool:
    """Buy one short back. True once the broker confirms a COMPLETE fill."""
    ts, ex, qty = job["tradingsymbol"], job["exchange"], job["qty"]
    price = kx.marketable_price(kite, ex, ts, BUY, qty, fallback=None,
                                cushion_ticks=cushion)
    if price is None:
        q = kx.quote_book(kite, ex, ts) or {}
        ltp = q.get("ltp")
        if not ltp:
            log(f"{ts}: no price at all — cannot size a limit, will retry")
            return False
        price = round(ltp * 2, 1)                      # last resort, still a LIMIT
    if dry:
        log(f"DRY-RUN would BUY {qty} {ts} @ {price} product={job['product']} "
            f"cushion={cushion}")
        return False
    try:
        oid = kx.place_limit_verified(kite, ts, ex, BUY, qty, price,
                                      job["product"], job["tag"])
    except Exception as e:
        log(f"{ts}: order REJECTED — {type(e).__name__}: {e}")
        audit.log(ts, "WATCHDOG_REJECTED", system="watchdog", error=str(e)[:200],
                  qty=qty, price=price, product=job["product"])
        return False
    log(f"{ts}: BUY {qty} @ {price} product={job['product']} oid={oid}")
    deadline = time.monotonic() + FILL_WAIT
    while time.monotonic() < deadline:
        st = kx.order_status(kite, oid)
        if st["status"] == "COMPLETE":
            log(f"{ts}: FILLED @ {st.get('avg_price')}")
            audit.log(ts, "WATCHDOG_CLOSED", system="watchdog", qty=qty,
                      avg=st.get("avg_price"), product=job["product"], oid=oid)
            return True
        if st["status"] in ("REJECTED", "CANCELLED"):
            log(f"{ts}: {st['status']} — re-pricing")
            return False
        time.sleep(0.5)
    try:
        kx.cancel(kite, oid)                           # never leave one resting
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what it WOULD close and place nothing")
    ap.add_argument("--rounds", type=int, default=len(CUSHIONS),
                    help="how many escalating attempts before giving up and shouting")
    ap.add_argument("--log", help="ALSO append this run to a log file, in UTF-8")
    a = ap.parse_args()
    if a.log:
        _tee(a.log)

    log(f"start{' (DRY RUN)' if a.dry_run else ''} — tags {', '.join(TAGS)}")
    try:
        kite = kx.get_kite()
    except Exception as e:
        log(f"FATAL: no Kite session — {e}")
        audit.log("ALL", "WATCHDOG_NO_BROKER", system="watchdog", error=str(e)[:200])
        return 2

    jobs = to_close(kite)
    if not jobs:
        log("nothing open that belongs to a strategy — clean")
        return 0

    log(f"{len(jobs)} strategy short(s) STILL OPEN after square-off:")
    for j in jobs:
        log(f"    {j['tradingsymbol']} qty={j['qty']} product={j['product']} tag={j['tag']}")
    audit.log("ALL", "WATCHDOG_FOUND_OPEN", system="watchdog", count=len(jobs),
              detail="; ".join(f"{j['tradingsymbol']}x{j['qty']}" for j in jobs))

    for rnd in range(a.rounds):
        cushion = CUSHIONS[min(rnd, len(CUSHIONS) - 1)]
        jobs = to_close(kite)                          # re-derive: some may have gone
        if not jobs:
            log("all closed — flat")
            audit.log("ALL", "WATCHDOG_FLAT", system="watchdog", rounds=rnd)
            return 0
        log(f"round {rnd + 1}/{a.rounds}, cushion {cushion} ticks")
        for j in jobs:
            close_one(kite, j, cushion, a.dry_run)
        if a.dry_run:
            return 0
        time.sleep(ROUND_PAUSE)

    left = to_close(kite)
    if not left:
        log("all closed — flat")
        audit.log("ALL", "WATCHDOG_FLAT", system="watchdog", rounds=a.rounds)
        return 0

    # Out of rounds and still short. Say so as loudly as a log file can: this is
    # an NRML position that will carry overnight unless a human acts.
    log("=" * 70)
    log("STILL SHORT AFTER EVERY ATTEMPT — CLOSE THESE BY HAND NOW:")
    for j in left:
        log(f"    {j['tradingsymbol']} qty={j['qty']} product={j['product']}")
    log("NRML does not auto-square-off. These WILL carry overnight.")
    log("=" * 70)
    audit.log("ALL", "WATCHDOG_FAILED", system="watchdog", count=len(left),
              detail="; ".join(f"{j['tradingsymbol']}x{j['qty']}" for j in left))
    return 1


if __name__ == "__main__":
    sys.exit(main())
