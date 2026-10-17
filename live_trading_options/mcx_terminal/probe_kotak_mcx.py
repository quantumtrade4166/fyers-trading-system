"""
mcx_terminal/probe_kotak_mcx.py — Stage 0: can Kotak P feed an MCX option chain?
================================================================================

READ-ONLY. Places NO order. Never prints tokens or credentials.

Checks, in order:
  1. Kotak Neo SDK is installed; Kotak P (env suffix _D) logs in.
  2. MCX scrip master (mcx_fo): columns, and for CRUDEOIL / GOLDM / SILVERM the option
     expiries, strike grid, lot size, freeze qty, tick size.
  3. REST quotes for the nearest-expiry chain (ATM +/- N strikes): what fields come back.
  4. Websocket: subscribe the same chain (+ depth on a few), count ticks per symbol for
     --secs seconds, and measure tick delay where the message carries a time.
  5. Order-feed websocket connects (no orders are placed, so no messages are expected).

Writes logs/mcx_probe_{stamp}.json (summary) and logs/mcx_scrip_{stamp}.csv (MCX options
subset of the scrip master) on the machine it runs on.

    .venv\\Scripts\\python.exe live_trading_options\\mcx_terminal\\probe_kotak_mcx.py --secs 60
"""
import csv
import datetime as dt
import io
import json
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "live_trading_options" / "strangle_strategy"))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAMES = ("CRUDEOIL", "GOLDM", "SILVERM")
STRIKES_EACH_SIDE = 10
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 45
STAMP = dt.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
OUT = {"stamp": STAMP, "ist_now": dt.datetime.now(IST).isoformat(), "steps": {}}


def step(name, **kv):
    OUT["steps"].setdefault(name, {}).update(kv)


def save():
    (LOGS / f"mcx_probe_{STAMP}.json").write_text(json.dumps(OUT, indent=2, default=str), encoding="utf-8")


def col(row_keys, *cands):
    """First column whose lower-case name equals / contains one of the candidates."""
    low = {k.lower(): k for k in row_keys}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    for c in cands:
        for lk, k in low.items():
            if c.lower() in lk:
                return k
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 1. SDK + login ───────────────────────────────────────────────────────────
def kotak_p_login():
    """Kotak P = env keys KOTAK_*_D. Own login (the VPS kotak_auth may predate the _D account)."""
    import os
    import pyotp
    from dotenv import load_dotenv
    from neo_api_client import NeoAPI
    load_dotenv(ROOT / "deployment" / ".env")
    req = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET")
    cr = {k: os.getenv(k + "_D") for k in req}
    missing = [k + "_D" for k, v in cr.items() if not v]
    if missing:
        raise RuntimeError("missing in deployment/.env: " + ", ".join(missing))
    m = cr["KOTAK_MOBILE"].strip()
    bare = m.lstrip("+")[-10:]
    last = None
    for mob in dict.fromkeys([m, "+91" + bare, bare]):
        c = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=cr["KOTAK_CONSUMER_KEY"])
        r1 = c.totp_login(mobile_number=mob, ucc=cr["KOTAK_UCC"],
                          totp=pyotp.TOTP(cr["KOTAK_TOTP_SECRET"].strip().replace(" ", "")).now())
        if not isinstance(r1, dict) or r1.get("error"):
            last = f"totp_login: {str(r1)[:160]}"
            continue
        r2 = c.totp_validate(mpin=cr["KOTAK_MPIN"])
        if not isinstance(r2, dict) or r2.get("error"):
            last = f"totp_validate: {str(r2)[:160]}"
            continue
        return c
    raise RuntimeError(last)


def login():
    try:
        import neo_api_client
        ver = getattr(neo_api_client, "__version__", "?")
        step("1_sdk", ok=True, version=ver, path=str(Path(neo_api_client.__file__).parent))
    except Exception as e:
        step("1_sdk", ok=False, error=f"{type(e).__name__}: {e}")
        raise SystemExit("Kotak SDK not installed")
    t0 = time.time()
    try:
        c = kotak_p_login()
    except Exception as e:
        step("1_login", ok=False, error=str(e)[:300])
        raise SystemExit(f"Kotak P login failed: {e}")
    step("1_login", ok=True, secs=round(time.time() - t0, 2))
    print(f"[1] Kotak P login OK ({time.time() - t0:.1f}s)")
    return c


# ── 2. scrip master ──────────────────────────────────────────────────────────
def scrip_master(c):
    import requests
    r = c.scrip_master(exchange_segment="mcx_fo")
    urls = []
    if isinstance(r, str):
        urls = [r]
    elif isinstance(r, dict):
        for v in r.values():
            if isinstance(v, str) and v.startswith("http"):
                urls.append(v)
            elif isinstance(v, dict):
                urls += [x for x in v.get("filesPaths", []) if "mcx" in x.lower()]
            elif isinstance(v, list):
                urls += [x for x in v if isinstance(x, str) and "mcx" in x.lower()]
    elif isinstance(r, list):
        urls = [x for x in r if isinstance(x, str)]
    step("2_scrip", raw_type=type(r).__name__, urls=urls, raw_head=str(r)[:300])
    if not urls:
        raise SystemExit(f"scrip_master gave no URL: {str(r)[:300]}")
    txt = requests.get(urls[0], timeout=60).text
    rows = list(csv.DictReader(io.StringIO(txt)))
    rows = [{(k or "").strip(): (v or "").strip() for k, v in x.items()} for x in rows]
    keys = list(rows[0].keys())
    step("2_scrip", rows=len(rows), columns=keys, sample=rows[:2])
    print(f"[2] scrip master mcx_fo: {len(rows)} rows, {len(keys)} columns")

    c_name = col(keys, "pSymbolName", "symbolname")
    c_trd = col(keys, "pTrdSymbol", "trdsymbol")
    c_tok = col(keys, "pSymbol")
    c_opt = col(keys, "pOptionType", "optiontype")
    c_strike = col(keys, "dStrikePrice;", "dStrikePrice", "strike")
    c_exp = col(keys, "pExpiryDate", "lExpiryDate", "expiry")
    c_lot = col(keys, "lLotSize", "lotsize")
    c_frz = col(keys, "lFreezeQty", "freeze")
    c_tick = col(keys, "dTickSize", "ticksize")
    c_inst = col(keys, "pInstType", "insttype")
    mapping = dict(name=c_name, trd=c_trd, token=c_tok, opt=c_opt, strike=c_strike, expiry=c_exp,
                   lot=c_lot, freeze=c_frz, tick=c_tick, inst=c_inst)
    step("2_scrip", column_map=mapping)

    keep = [x for x in rows if x.get(c_name, "").upper() in NAMES or
            any(x.get(c_trd, "").upper().startswith(n) for n in NAMES)]
    with open(LOGS / f"mcx_scrip_{STAMP}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(keep)

    per = {}
    for n in NAMES:
        mine = [x for x in keep if x.get(c_name, "").upper() == n or x.get(c_trd, "").upper().startswith(n)]
        opts = [x for x in mine if x.get(c_opt, "").upper() in ("CE", "PE")]
        futs = [x for x in mine if x.get(c_opt, "").upper() not in ("CE", "PE")]
        exps = sorted({x.get(c_exp) for x in opts}, key=lambda e: fnum(e) or 0)
        per[n] = {
            "rows": len(mine), "options": len(opts), "futures": len(futs),
            "expiries_raw": exps[:6],
            "lot_size": sorted({x.get(c_lot) for x in mine})[:5],
            "freeze_qty": sorted({x.get(c_frz) for x in mine})[:5] if c_frz else "no column",
            "tick": sorted({x.get(c_tick) for x in mine})[:5],
            "fut_sample": [{k: x.get(v) for k, v in mapping.items() if v} for x in futs[:3]],
            "opt_sample": [{k: x.get(v) for k, v in mapping.items() if v} for x in opts[:3]],
        }
        print(f"    {n:<9} options={len(opts):<5} futures={len(futs):<3} expiries={len(exps)} "
              f"lot={per[n]['lot_size']} freeze={per[n]['freeze_qty']}")
    step("2_scrip", per_instrument=per)
    return keep, mapping


def nearest_chain(keep, m, name, ref_price):
    """Nearest-expiry options of `name`, ATM +/- N strikes (by the ref price)."""
    opts = [x for x in keep if (x.get(m["name"], "").upper() == name or x.get(m["trd"], "").upper().startswith(name))
            and x.get(m["opt"], "").upper() in ("CE", "PE")]
    if not opts:
        return []
    e0 = min({fnum(x.get(m["expiry"])) or 0 for x in opts})
    opts = [x for x in opts if (fnum(x.get(m["expiry"])) or 0) == e0]
    strikes = sorted({fnum(x.get(m["strike"])) for x in opts if fnum(x.get(m["strike"])) is not None})
    if not strikes:
        return []
    if ref_price is None:
        mid = strikes[len(strikes) // 2]
    else:
        # the scrip master may store strikes scaled (x100); pick the scale that fits the price
        scale = min((1, 100, 10000), key=lambda s: abs(strikes[len(strikes) // 2] / s - ref_price))
        mid = min(strikes, key=lambda s: abs(s / scale - ref_price))
    i = strikes.index(mid)
    want = set(strikes[max(0, i - STRIKES_EACH_SIDE): i + STRIKES_EACH_SIDE + 1])
    return [x for x in opts if fnum(x.get(m["strike"])) in want]


# ── 3. REST quotes ───────────────────────────────────────────────────────────
def rest_quotes(c, toks):
    attempts = (lambda: c.quotes(instrument_tokens=toks, quote_type="all"),
                lambda: c.quotes(instrument_tokens=toks, quote_type="ltp"),
                lambda: c.quotes(instrument_tokens=toks))
    for i, fn in enumerate(attempts):
        try:
            t0 = time.time()
            r = fn()
            ms = round((time.time() - t0) * 1000)
            return r, ms, i
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return {"error": last}, None, None


def fut_price(c, keep, m, name):
    futs = [x for x in keep if (x.get(m["name"], "").upper() == name or x.get(m["trd"], "").upper().startswith(name))
            and x.get(m["opt"], "").upper() not in ("CE", "PE")]
    if not futs:
        return None, None
    f = min(futs, key=lambda x: fnum(x.get(m["expiry"])) or 9e18)
    r, _, _ = rest_quotes(c, [{"instrument_token": f[m["token"]], "exchange_segment": "mcx_fo"}])
    txt = json.dumps(r, default=str)
    for k in ("ltp", "last_traded_price", "ltP", "lp"):
        try:
            d = r if isinstance(r, list) else r.get("message", r.get("data", r))
            d = d[0] if isinstance(d, list) and d else d
            v = fnum(d.get(k)) if isinstance(d, dict) else None
            if v:
                return v, f
        except Exception:
            pass
    return None, {"fut": f.get(m["trd"]), "raw": txt[:400]}


# ── 4. websocket ticks ───────────────────────────────────────────────────────
TIME_KEYS = ("ltt", "fdtm", "ltd", "tvalue", "ft", "exchange_time", "last_traded_time")


def websocket_test(c, toks, depth_toks):
    counts = defaultdict(int)
    first_seen = {}
    samples = []
    delays = []
    errors = []
    events = []
    lock = threading.Lock()

    def on_message(msg):
        now = time.time()
        with lock:
            if len(samples) < 8:
                samples.append(msg)
            data = msg.get("data") if isinstance(msg, dict) else msg
            items = data if isinstance(data, list) else [data]
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = str(it.get("tk") or it.get("instrument_token") or it.get("token") or "?")
                counts[tk] += 1
                first_seen.setdefault(tk, now)
                for k in TIME_KEYS:
                    v = fnum(it.get(k))
                    if v and v > 1e9:
                        v = v / 1000 if v > 1e12 else v
                        delays.append((k, round((now - v) * 1000)))
                        break

    def on_error(e):
        errors.append(str(e)[:200])

    def on_open(m=None):
        events.append(("open", time.time()))

    def on_close(m=None):
        events.append(("close", time.time()))

    c.on_message = on_message
    c.on_error = on_error
    c.on_open = on_open
    c.on_close = on_close

    t0 = time.time()
    try:
        c.subscribe(instrument_tokens=toks, isIndex=False, isDepth=False)
    except Exception as e:
        errors.append(f"subscribe: {type(e).__name__}: {e}")
    if depth_toks:
        try:
            c.subscribe(instrument_tokens=depth_toks, isIndex=False, isDepth=True)
        except Exception as e:
            errors.append(f"subscribe depth: {type(e).__name__}: {e}")
    print(f"[4] websocket subscribed {len(toks)} symbols (+{len(depth_toks)} depth); listening {SECS}s ...")
    time.sleep(SECS)
    try:
        c.un_subscribe(instrument_tokens=toks, isIndex=False, isDepth=False)
    except Exception:
        pass

    total = sum(counts.values())
    d_ms = sorted(x[1] for x in delays)
    res = {
        "secs": SECS, "symbols_subscribed": len(toks), "symbols_ticking": len(counts),
        "ticks_total": total, "ticks_per_sec": round(total / max(SECS, 1), 1),
        "first_tick_after_ms": round((min(first_seen.values()) - t0) * 1000) if first_seen else None,
        "delay_field": delays[0][0] if delays else None,
        "delay_ms_p50": d_ms[len(d_ms) // 2] if d_ms else None,
        "delay_ms_p90": d_ms[int(len(d_ms) * 0.9)] if d_ms else None,
        "errors": errors[:10], "events": [(e, round(t - t0, 2)) for e, t in events],
        "samples": samples,
    }
    step("4_websocket", **res)
    print(f"    ticking {len(counts)}/{len(toks)} symbols, {total} ticks "
          f"({res['ticks_per_sec']}/s), first after {res['first_tick_after_ms']} ms, "
          f"delay p50={res['delay_ms_p50']} p90={res['delay_ms_p90']} ms ({res['delay_field']})")
    if errors:
        print("    errors:", errors[:3])


# ── 5. order feed ────────────────────────────────────────────────────────────
def order_feed(c):
    try:
        c.subscribe_to_orderfeed()
        time.sleep(5)
        step("5_orderfeed", ok=True, note="connected; no orders placed so no messages expected")
        print("[5] order feed subscribe OK")
    except Exception as e:
        step("5_orderfeed", ok=False, error=f"{type(e).__name__}: {e}")
        print(f"[5] order feed FAILED: {e}")


def main():
    n = dt.datetime.now(IST)
    mcx_open = n.weekday() < 5 and dt.time(9, 0) <= n.time() <= dt.time(23, 30)
    OUT["mcx_open_guess"] = mcx_open
    print(f"Kotak P MCX probe — {n:%a %d-%b %H:%M} IST — MCX {'OPEN' if mcx_open else 'CLOSED (expect no ticks)'}\n")
    try:
        c = login()
        keep, m = scrip_master(c)

        toks, depth = [], []
        for name in NAMES:
            ref, info = fut_price(c, keep, m, name)
            step("3_futures", **{name: {"ref_price": ref, "info": info if ref is None else None}})
            chain = nearest_chain(keep, m, name, ref)
            tt = [{"instrument_token": x[m["token"]], "exchange_segment": "mcx_fo"} for x in chain]
            toks += tt
            depth += tt[:2]
            print(f"[3] {name:<9} future ref={ref}  chain symbols={len(chain)}")
        r, ms, variant = rest_quotes(c, toks[:20])
        step("3_quotes", ms_for_20=ms, call_variant=variant, sample=str(r)[:1500])
        print(f"[3] REST quotes for 20 symbols: {ms} ms (variant {variant})")
        save()

        websocket_test(c, toks, depth)
        save()
        order_feed(c)
    finally:
        save()
        print(f"\nsummary -> logs/mcx_probe_{STAMP}.json")


if __name__ == "__main__":
    main()
