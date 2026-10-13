"""
eod_flatten.py — STANDALONE end-of-day flatten safety net (Zerodha / Kite).
============================================================================

Independent of every strategy engine and its own-book. Reads the BROKER's real
net positions directly and covers every open SHORT option leg, verifying against
the broker and retrying through API timeouts. The backstop for the 2026-10-12
failure (an engine marked itself flat while a leg was really still open overnight).

Scope / safety:
  - Broker-truth only: acts on kite.positions() net, never on any engine's record.
  - Covers SHORTS only (the real risk: a naked short carried into expiry/overnight).
    LONG option legs are LEFT ALONE — they expire worthless with no assignment risk,
    and selling them would needlessly unhedge a still-open short.
  - As the LAST line of defence it is authoritative: it CANCELS any resting order on
    a short's symbol (a stuck engine cover included) and then places its own
    aggressive marketable cover — so a stuck order can never block the flatten, and
    cancel-then-place-one means it never double-covers.
  - Re-reads the broker after cancelling so it covers the true remaining qty.
  - Marketable-limit priced off the live book (Zerodha rejects MARKET on options).
  - Sanity cap: refuses to auto-trade a leg whose qty looks absurd (> 5000).
  - Idempotent: no open short -> no-op. Loops ~5 min then ALARMS if not flat.
  - EOD_DRY=1 -> logs what it WOULD do and places/cancels NOTHING (safe testing).

Run once ~15:16 IST (after the engine's 15:14 square-off, before the 15:20 watchdog).
"""
import sys, os, time
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                       # so `from live import ...` resolves
from live import kite_executor as kx

TAG = "eodflat"
DRY = os.environ.get("EOD_DRY") == "1"
LOGDIR = os.path.abspath(os.path.join(HERE, "..", "..", "logs"))
LOGFILE = os.path.join(LOGDIR, f"eod_flatten_{dt.date.today()}.log")
RESTING = ("OPEN", "TRIGGER PENDING", "OPEN PENDING", "VALIDATION PENDING",
           "PUT ORDER REQ RECEIVED", "MODIFY VALIDATION PENDING")


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {'[DRY] ' if DRY else ''}{msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def open_legs(k):
    """Open NIFTY/SENSEX OPTION positions at the broker (qty != 0)."""
    out = []
    for p in (k.positions() or {}).get("net", []):
        ts = p.get("tradingsymbol", "") or ""
        q = p.get("quantity") or 0
        if q == 0 or not any(x in ts for x in ("NIFTY", "SENSEX")):
            continue
        if not (ts.endswith("CE") or ts.endswith("PE")):
            continue
        if abs(q) > 5000:
            log(f"SKIP {ts} qty={q} exceeds sanity cap 5000 — MANUAL CHECK, not auto-trading")
            continue
        out.append(p)
    return out


def _cover_price(k, exch, ts, qty):
    key = f"{exch}:{ts}"
    try:
        ltp = (k.quote([key]) or {}).get(key, {}).get("last_price") or 0
    except Exception:
        ltp = 0
    fb = round(max(0.1, ltp) * 3 + 10, 1)                      # aggressive buy fallback
    return kx.marketable_price(k, exch, ts, "BUY", qty, fallback=fb, cushion_ticks=5) or fb


def flatten_pass(k):
    """One sweep. Returns True when the broker shows NO open SHORT option leg."""
    legs = open_legs(k)
    for p in legs:
        if (p.get("quantity") or 0) > 0:
            log(f"leaving LONG {p['tradingsymbol']} qty={p['quantity']} (expires worthless / no assignment risk)")
    shorts = [p for p in legs if (p.get("quantity") or 0) < 0]
    if not shorts:
        return True
    syms = {p["tradingsymbol"] for p in shorts}
    # cancel ANY resting order on these symbols (stuck engine covers included) so nothing
    # blocks us and we never end up double-covering.
    for o in (k.orders() or []):
        if o.get("tradingsymbol") in syms and o.get("status") in RESTING:
            if DRY:
                log(f"would cancel resting {o['order_id']} {o.get('tradingsymbol')} tag={o.get('tag')} {o.get('transaction_type')}")
            else:
                try:
                    kx.cancel(k, o["order_id"])
                    log(f"cancelled resting {o['order_id']} {o.get('tradingsymbol')} tag={o.get('tag')}")
                except Exception as e:
                    log(f"cancel failed {o['order_id']}: {e}")
    if not DRY:
        time.sleep(1.0)                                        # let cancels (and any fill) settle
    # re-read the true remaining shorts, then cover each
    current = shorts if DRY else [p for p in open_legs(k) if (p.get("quantity") or 0) < 0]
    for p in sorted(current, key=lambda x: x["tradingsymbol"]):
        ts, exch = p["tradingsymbol"], p.get("exchange")
        qty, product = abs(p.get("quantity") or 0), (p.get("product") or "NRML")
        px = _cover_price(k, exch, ts, qty)
        if DRY:
            log(f"would BUY {qty} {ts} @ {px} to cover (net {p.get('quantity')}, product {product})")
            continue
        try:
            oid = kx.place_limit(k, ts, exch, "BUY", qty, px, product=product, tag=TAG)
            log(f"COVER BUY {qty} {ts} @ {px} (net was {p.get('quantity')}) oid={oid}")
        except Exception as e:
            log(f"COVER FAILED {qty} {ts}: {e}")
    return False


def main():
    log(f"=== EOD flatten safety net START (now {dt.datetime.now():%H:%M:%S} IST) ===")
    try:
        k = kx.get_kite()
    except Exception as e:
        log(f"*** CANNOT LOGIN KITE: {e} — CHECK BROKER MANUALLY ***")
        return 2
    deadline = time.monotonic() + 300          # ~5-minute window
    while time.monotonic() < deadline:
        try:
            if flatten_pass(k):
                log("=== ALL SHORT LEGS FLAT — DONE ===")
                return 0
        except Exception as e:
            log(f"pass error (retry): {e}")
        if DRY:
            break
        time.sleep(10)
    try:
        still = [p for p in open_legs(k) if (p.get("quantity") or 0) < 0]
    except Exception as e:
        log(f"*** ALARM: final broker read failed: {e} — CHECK BROKER MANUALLY ***")
        return 1
    if not still:
        log("=== ALL SHORT LEGS FLAT (final) — DONE ===")
        return 0
    log("*** ALARM: SHORT LEG(S) STILL OPEN AFTER WINDOW: "
        + ", ".join(f"{p['tradingsymbol']}:{p['quantity']}" for p in still) + " — CHECK BROKER ***")
    return 1


if __name__ == "__main__":
    sys.exit(main())
