"""
strategy_builder/chain_feed.py — live NIFTY / SENSEX option chain for the builder.
================================================================================

Two sources, merged:
  1. Fyers REST `optionchain` (greeks=1, 50 strikes each side) per WATCHED expiry,
     re-pulled every REST_REFRESH_S. Gives the strike list, symbols, OI, OI change,
     volume, bid/ask and the futures price.
  2. Fyers WebSocket SymbolUpdate on every symbol of every watched chain. Gives the
     tick-by-tick LTP / bid / ask between REST pulls.

IV and Greeks are recomputed on EVERY snapshot from the live prices (Black-76 on the
synthetic forward, greeks.py) — so they move with the ticks instead of being 15 s old.

"Watched" = an (index, expiry) a page asked for in the last WATCH_TTL_S. The nearest
expiry of each index is always watched. A page that opens a far expiry costs one REST
pull per 15 s while it is open and nothing once it is closed.

This module only READS market data. It never logs in (it reads the token file the VPS
writes at 09:00), never trades and never touches another engine's state.
"""
import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from live_trading_options.strategy_builder import config as C
from live_trading_options.strategy_builder import greeks as G

log = logging.getLogger("sb.chain")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TOKEN_FILE = C.ROOT / "config" / "access_token.txt"
LAST_FILE = C.DATA / "last_chain.json"      # last good chain -> shown as "last close" when Fyers can't answer
PERSIST_EVERY = 60
CLIENT_ID = "W09OMXQB8J-100"


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _f(v):
    try:
        x = float(v)
        return x if np.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _expiry_dt(date_str: str) -> dt.datetime:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    return dt.datetime(d.year, d.month, d.day, *C.EXPIRY_HOUR, tzinfo=IST)


def years_to(expiry: str, at: dt.datetime | None = None) -> float:
    at = at or now_ist()
    return max((_expiry_dt(expiry) - at).total_seconds(), 0.0) / (365.0 * 86400)


class ChainFeed:
    def __init__(self, load_cache: bool = True):
        self._lock = threading.RLock()
        self._rest = None
        self._token_sig = None          # (mtime, size) of the token file the clients were built from
        self._ws = None
        self._ws_subscribed: set[str] = set()
        self._ws_connected = False
        self.last_tick_at: float | None = None

        self.expiries: dict[str, list[dict]] = {}          # index -> [{date, epoch, flag, dte}]
        self.watch: dict[tuple, float] = {}                # (index, expiry date) -> last asked (monotonic)
        self.chains: dict[tuple, dict] = {}                # (index, expiry) -> REST chain
        self.live: dict[str, dict] = {}                    # symbol -> {ltp, bid, ask, t}
        self.under: dict[str, dict] = {}                   # index -> {spot, fut, prev_close, ...}
        self.vix: dict = {}
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._persisted = 0.0
        self.rest_day, self.rest_calls, self.rest_blocked = None, 0, None
        self._want_idx: set = set()
        self._exp_day: dict = {}
        self.cache_as_of: str | None = None     # set while the chain on screen is the saved last close
        if load_cache:
            self._load_last()

    # ── last close on disk (weekends, holidays, an expired token, a Fyers outage) ──
    def _persist(self):
        if time.time() - self._persisted < PERSIST_EVERY:
            return
        self._persisted = time.time()
        with self._lock:
            chains = {}
            for (idx, exp), ch in self.chains.items():
                rows = []
                for r in ch["by_symbol"].values():
                    ltp, bid, ask = self._px(r)
                    rows.append({**r, "ltp": ltp, "bid": bid, "ask": ask})
                chains[f"{idx}|{exp}"] = rows
            under = {}
            for i in C.INDICES:
                u = dict(self.under.get(i, {}))
                lv = self.live.get(C.INDEX_SYMBOL[i]) or {}
                if lv.get("ltp"):
                    u["spot"] = lv["ltp"]
                under[i] = u
            doc = {"saved_at": now_ist().strftime("%Y-%m-%d %H:%M:%S"), "expiries": self.expiries,
                   "under": under, "vix": self.vix, "chains": chains}
        try:
            C.DATA.mkdir(parents=True, exist_ok=True)
            tmp = LAST_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(LAST_FILE)
        except Exception as e:
            log.info("persist last chain: %s", e)

    def _load_last(self):
        try:
            doc = json.loads(LAST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        today = now_ist().date().isoformat()
        self.cache_as_of = doc.get("saved_at")
        as_of = dt.datetime.fromisoformat(self.cache_as_of).replace(tzinfo=IST) if self.cache_as_of else None
        self.expiries = {i: [e for e in ex if e["date"] >= today] for i, ex in (doc.get("expiries") or {}).items()}
        self.under = {i: u for i, u in (doc.get("under") or {}).items() if u}
        self.vix = doc.get("vix") or {}
        for key, rows in (doc.get("chains") or {}).items():
            idx, exp = key.split("|")
            if exp < today:
                continue
            by_symbol, by_strike = {}, {}
            for r in rows:
                by_symbol[r["symbol"]] = r
                by_strike.setdefault(r["strike"], {})[r["type"]] = r
            self.chains[(idx, exp)] = {"by_symbol": by_symbol, "by_strike": by_strike, "fetched": 0.0,
                                       "as_of": as_of}
        log.info("loaded last close from %s (%d chains)", self.cache_as_of, len(self.chains))

    # ── auth ────────────────────────────────────────────────────────────────
    def _token(self) -> str:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["token"]

    def _ensure_clients(self):
        """(Re)build the REST client + WebSocket when the token file changes (the VPS
        rewrites it at 09:00). Reading a file never invalidates anyone's token."""
        st = TOKEN_FILE.stat()
        sig = (st.st_mtime, st.st_size)
        if sig == self._token_sig and self._rest is not None:
            return
        tok = self._token()
        self._rest = fyersModel.FyersModel(client_id=CLIENT_ID, token=tok,
                                           log_path=str(C.LOG_DIR), is_async=False)
        self._token_sig = sig
        self._start_ws(tok)
        log.info("fyers clients built from token file (mtime %s)", dt.datetime.fromtimestamp(sig[0]))

    def _start_ws(self, tok: str):
        old = self._ws
        if old is not None:
            try:
                old.close_connection()
            except Exception:
                pass
        self._ws_subscribed = set()
        self._ws_connected = False
        ws = data_ws.FyersDataSocket(
            access_token=f"{CLIENT_ID}:{tok}", log_path="", litemode=False,
            write_to_file=False, reconnect=True,
            on_connect=self._on_open, on_close=self._on_close,
            on_error=self._on_error, on_message=self._on_message)
        self._ws = ws
        threading.Thread(target=ws.connect, daemon=True, name="SB-FyersWS").start()

    # ── websocket callbacks ─────────────────────────────────────────────────
    def _on_open(self):
        self._ws_connected = True
        with self._lock:
            syms = sorted(self._wanted_symbols())
        self._ws_subscribed = set()
        self._subscribe(syms)
        log.info("ws connected, %d symbols", len(syms))

    def _on_close(self, m):
        self._ws_connected = False
        log.warning("ws closed: %s", m)

    def _on_error(self, m):
        log.warning("ws error: %s", m)

    def _on_message(self, msg):
        ticks = []
        if isinstance(msg, dict):
            d = msg.get("d")
            ticks = d if isinstance(d, list) else [d] if isinstance(d, dict) else [msg]
        elif isinstance(msg, list):
            ticks = msg
        now = time.time()
        with self._lock:
            for t in ticks:
                if not isinstance(t, dict):
                    continue
                sym = t.get("symbol")
                ltp = _f(t.get("ltp"))
                if not sym or ltp is None:
                    continue
                rec = self.live.setdefault(sym, {})
                rec["ltp"] = ltp
                b, a = _f(t.get("bid_price")), _f(t.get("ask_price"))
                if b is not None:
                    rec["bid"] = b
                if a is not None:
                    rec["ask"] = a
                pc = _f(t.get("prev_close_price"))
                if pc:
                    rec["prev_close"] = pc
                rec["t"] = now
            self.last_tick_at = now

    def _subscribe(self, syms):
        new = [s for s in syms if s not in self._ws_subscribed]
        if not new or self._ws is None or not self._ws_connected:
            return
        for i in range(0, len(new), 200):
            chunk = new[i:i + 200]
            try:
                self._ws.subscribe(symbols=chunk, data_type="SymbolUpdate")
                self._ws_subscribed.update(chunk)
            except Exception as e:
                self._err(f"ws subscribe: {e}")

    def _wanted_symbols(self) -> set[str]:
        out = set(C.INDEX_SYMBOL.values()) | {C.VIX_SYMBOL}
        for ch in self.chains.values():
            out.update(ch["by_symbol"].keys())
        return out

    # ── REST ────────────────────────────────────────────────────────────────
    def _err(self, s):
        log.warning(s)
        with self._lock:
            self.errors = (self.errors + [f"{now_ist():%H:%M:%S} {s}"])[-20:]

    def _pull(self, index: str, epoch: str | None):
        day = now_ist().date().isoformat()
        if self.rest_day != day:
            self.rest_day, self.rest_calls, self.rest_blocked = day, 0, None
        if self.rest_blocked:
            raise RuntimeError(f"REST paused for today: {self.rest_blocked}")
        if self.rest_calls >= C.DAILY_REST_CAP:
            self.rest_blocked = f"builder used its {C.DAILY_REST_CAP} Fyers calls for today (ticks still stream)"
            raise RuntimeError(self.rest_blocked)
        self.rest_calls += 1
        r = self._rest.optionchain(data={"symbol": C.INDEX_SYMBOL[index], "strikecount": C.STRIKECOUNT,
                                         "timestamp": epoch or "", "greeks": 1})
        if not isinstance(r, dict) or r.get("s") != "ok":
            if isinstance(r, dict) and (r.get("code") == -360 or "limit" in str(r.get("message", "")).lower()):
                self.rest_blocked = "Fyers daily request limit reached — waiting for tomorrow (ticks still stream)"
            raise RuntimeError(f"optionchain {index} {epoch}: {str(r)[:200]}")
        return r["data"]

    def _ingest_expiries(self, index: str, data: dict):
        out = []
        today = now_ist().date()
        for e in data.get("expiryData") or []:
            try:
                d = dt.datetime.strptime(e["date"], "%d-%m-%Y").date()
            except Exception:
                continue
            out.append({"date": d.isoformat(), "epoch": str(e.get("expiry")),
                        "flag": e.get("expiry_flag"), "dte": (d - today).days})
        out.sort(key=lambda x: x["date"])
        if out:
            self.expiries[index] = out

    def _ingest_chain(self, index: str, expiry: str, data: dict):
        by_symbol, by_strike = {}, {}
        und = {}
        for r in data.get("optionsChain") or []:
            ot = (r.get("option_type") or "").upper()
            if ot not in ("CE", "PE"):
                und = {"spot": _f(r.get("ltp")), "fut": _f(r.get("fp")),
                       "chg": _f(r.get("ltpch")), "chgp": _f(r.get("ltpchp")),
                       "symbol": r.get("symbol")}
                continue
            k = _f(r.get("strike_price"))
            g = r.get("greeks") or {}
            row = {"symbol": r.get("symbol"), "strike": k, "type": ot,
                   "ltp": _f(r.get("ltp")), "bid": _f(r.get("bid")), "ask": _f(r.get("ask")),
                   "oi": _f(r.get("oi")), "oich": _f(r.get("oich")), "volume": _f(r.get("volume")),
                   "chg": _f(r.get("ltpch")), "f_iv": _f(g.get("iv")), "f_delta": _f(g.get("delta"))}
            by_symbol[row["symbol"]] = row
            by_strike.setdefault(k, {})[ot] = row
        vixn = data.get("indiavixData") or {}
        with self._lock:
            self.chains[(index, expiry)] = {"by_symbol": by_symbol, "by_strike": by_strike,
                                            "fetched": time.time()}
            self.cache_as_of = None                 # a fresh pull: no longer showing the saved close
            if und:
                self.under[index] = {**self.under.get(index, {}), **und, "rest_at": time.time()}
            if vixn:
                self.vix = {"ltp": _f(vixn.get("ltp")), "chg": _f(vixn.get("ltpch"))}
            syms = sorted(self._wanted_symbols())
        self._subscribe(syms)
        self._persist()

    def _expiry_epoch(self, index, expiry):
        for e in self.expiries.get(index, []):
            if e["date"] == expiry:
                return e["epoch"]
        return None

    def refresh_once(self):
        """One maintenance pass — ONLY for what a page has asked for in the last WATCH_TTL_S.
        Nobody looking = zero Fyers REST calls (the websocket keeps streaming open legs)."""
        self._ensure_clients()
        now_m = time.monotonic()
        today = now_ist().date().isoformat()
        with self._lock:
            for k, seen in list(self.watch.items()):
                if now_m - seen > C.WATCH_TTL_S:
                    del self.watch[k]                    # keep the last chain on screen, just stop pulling it
            wanted_idx = {i for i, _ in self.watch} | set(self._want_idx)
        for index in C.INDICES:
            ex = self.expiries.get(index) or []
            stale = not ex or ex[0]["date"] < today or self._exp_day.get(index) != today
            if index in wanted_idx and stale:
                data = self._pull(index, None)            # expiry list: once a day per index
                self._ingest_expiries(index, data)
                self._exp_day[index] = today
                ex = self.expiries.get(index)
                if ex:
                    self._ingest_chain(index, ex[0]["date"], data)
                time.sleep(0.25)
        self._want_idx.clear()
        with self._lock:
            todo = [k for k in self.watch
                    if time.time() - self.chains.get(k, {}).get("fetched", 0) >= C.REST_REFRESH_S]
        for index, expiry in todo:
            epoch = self._expiry_epoch(index, expiry)
            if epoch is None:
                continue
            try:
                data = self._pull(index, epoch)
                self._ingest_expiries(index, data)
                self._ingest_chain(index, expiry, data)
            except Exception as e:
                self._err(str(e))
            time.sleep(0.25)                 # stays far under Fyers' 10 req/s, shared with the engines

    def run(self):
        fails = 0
        while not self._stop.is_set():
            try:
                self.refresh_once()
                fails = 0
            except Exception as e:
                fails += 1
                if fails <= 3 or fails % 20 == 0:       # an expired weekend token must not flood the log
                    self._err(f"refresh: {type(e).__name__}: {e}")
                # back off 5 s, 10 s ... up to 2 min; the 09:00 token file change resets it
                self._stop.wait(min(120, 5 * fails))
                if self._token_sig and TOKEN_FILE.exists():
                    st = TOKEN_FILE.stat()
                    if (st.st_mtime, st.st_size) != self._token_sig:
                        fails = 0
                continue
            self._stop.wait(1.0)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="SB-chain").start()

    # ── reads (called by the API) ───────────────────────────────────────────
    def touch(self, index: str, expiry: str):
        with self._lock:
            self.watch[(index, expiry)] = time.monotonic()

    def want_index(self, index: str):
        """A page opened an index whose expiry list is not known yet."""
        self._want_idx.add(index)

    def _px(self, row):
        """Live tick if we have one, else the REST value."""
        lv = self.live.get(row["symbol"]) or {}
        ltp = lv.get("ltp", row["ltp"])
        bid = lv.get("bid", row["bid"])
        ask = lv.get("ask", row["ask"])
        return ltp, bid, ask

    def underlying(self, index: str) -> dict:
        with self._lock:
            u = dict(self.under.get(index, {}))
            lv = self.live.get(C.INDEX_SYMBOL[index]) or {}
            if lv.get("ltp"):
                u["spot"] = lv["ltp"]
                if lv.get("prev_close"):
                    u["chg"] = lv["ltp"] - lv["prev_close"]
                    u["chgp"] = 100 * u["chg"] / lv["prev_close"]
            vx = dict(self.vix)
            lv = self.live.get(C.VIX_SYMBOL) or {}
            if lv.get("ltp"):
                vx["ltp"] = lv["ltp"]
                if lv.get("prev_close"):
                    vx["chg"] = lv["ltp"] - lv["prev_close"]
        return {**u, "vix": vx}

    def now(self) -> dt.datetime:
        """The feed's clock. Practice mode (practice.py) overrides it with the replay clock."""
        return now_ist()

    def snapshot(self, index: str, expiry: str) -> dict:
        """Chain rows with live prices + IV/Greeks recomputed now."""
        self.touch(index, expiry)
        with self._lock:
            ch = self.chains.get((index, expiry))
            if not ch:
                return {"ok": False, "loading": True, "index": index, "expiry": expiry}
            strikes = sorted(ch["by_strike"])
            rows = []
            for k in strikes:
                pair = ch["by_strike"][k]
                rec = {"strike": k}
                for ot in ("CE", "PE"):
                    r = pair.get(ot)
                    if r:
                        ltp, bid, ask = self._px(r)
                        rec[ot] = {"symbol": r["symbol"], "ltp": ltp, "bid": bid, "ask": ask,
                                   "oi": r["oi"], "oich": r["oich"], "volume": r["volume"]}
                rows.append(rec)
            fetched = ch["fetched"]
            as_of = ch.get("as_of")
        u = self.underlying(index)
        fwd = self._synthetic_forward(rows, u.get("fut") or u.get("spot"))
        T = years_to(expiry, as_of or self.now())     # a saved close keeps the IV of ITS moment
        self._add_greeks(rows, fwd, T)
        # ATM = strike nearest SPOT (what the owner trades against; options settle on spot).
        # The synthetic forward is still used above for IV / Greeks — it is what premiums price off.
        ref = u.get("spot") or fwd
        atm = min(strikes, key=lambda k: abs(k - ref)) if strikes and ref else None
        return {"ok": True, "index": index, "expiry": expiry, "lot": C.LOT_SIZE[index],
                "step": C.STRIKE_STEP[index], "under": u, "synthetic_fut": fwd, "atm": atm,
                "T_days": T * 365, "rows": rows, "rest_age": round(time.time() - fetched, 1),
                "tick_age": round(time.time() - self.last_tick_at, 1) if self.last_tick_at else None,
                "last_close": as_of.strftime("%a %d-%b %H:%M") if as_of else None}

    @staticmethod
    def _mid(o):
        if not o:
            return None
        b, a = o.get("bid"), o.get("ask")
        if b and a and a >= b > 0 and (a - b) <= max(2.0, 0.1 * a):
            return 0.5 * (a + b)
        return o.get("ltp")

    def _synthetic_forward(self, rows, ref):
        """F = K + C - P at the strike nearest the futures price (put-call parity).
        Averages the 3 strikes around it so one stale print cannot move it."""
        if not rows or not ref:
            return ref
        near = sorted(rows, key=lambda r: abs(r["strike"] - ref))[:3]
        vals = []
        for r in near:
            c, p = self._mid(r.get("CE")), self._mid(r.get("PE"))
            if c and p:
                vals.append(r["strike"] + c - p)
        return float(np.median(vals)) if vals else ref

    def _add_greeks(self, rows, F, T):
        if not F:
            return
        items = [(r, ot) for r in rows for ot in ("CE", "PE") if r.get(ot)]
        if not items:
            return
        K = np.array([r["strike"] for r, _ in items])
        is_call = np.array([ot == "CE" for _, ot in items])
        prem = np.array([self._mid(r[ot]) or np.nan for r, ot in items], dtype=float)
        iv = G.implied_vol(prem, F, K, T, is_call)
        # deep ITM rows often have no clean vol: borrow the other side's (same strike) vol
        by_k = {}
        for (r, ot), v in zip(items, iv):
            if np.isfinite(v):
                by_k.setdefault(r["strike"], []).append(v)
        iv = np.array([v if np.isfinite(v) else (np.mean(by_k[r["strike"]]) if r["strike"] in by_k else np.nan)
                       for (r, _), v in zip(items, iv)])
        gr = G.greeks(F, K, T, np.where(np.isfinite(iv), iv, 0.15), is_call)
        for i, (r, ot) in enumerate(items):
            o = r[ot]
            ok = bool(np.isfinite(iv[i]))
            o["iv"] = round(float(iv[i]) * 100, 2) if ok else None
            if ok:
                o["delta"] = round(float(gr["delta"][i]), 3)
            else:                                   # no vol: deep ITM ~ +/-1, far OTM ~ 0
                itm = (r["strike"] < F) if ot == "CE" else (r["strike"] > F)
                o["delta"] = (1.0 if ot == "CE" else -1.0) if itm else 0.0
            o["gamma"] = float(gr["gamma"][i]) if ok else 0.0
            o["theta"] = round(float(gr["theta"][i]), 2) if ok else 0.0
            o["vega"] = round(float(gr["vega"][i]), 2) if ok else 0.0

    def quotes(self, symbols: list[str]) -> dict:
        """LTP/bid/ask for any symbols (playground legs), live where subscribed."""
        out = {}
        with self._lock:
            index_of = {}
            for (idx, exp), ch in self.chains.items():
                for s, r in ch["by_symbol"].items():
                    index_of[s] = r
            for s in symbols:
                r = index_of.get(s)
                if r:
                    ltp, bid, ask = self._px(r)
                    out[s] = {"ltp": ltp, "bid": bid, "ask": ask}
                elif s in self.live:
                    lv = self.live[s]
                    out[s] = {"ltp": lv.get("ltp"), "bid": lv.get("bid"), "ask": lv.get("ask")}
            missing = [s for s in symbols if s not in out]
        if missing:
            self._subscribe(missing)
        return out

    def status(self) -> dict:
        with self._lock:
            return {"rest_calls_today": self.rest_calls, "rest_cap": C.DAILY_REST_CAP, "rest_blocked": self.rest_blocked,
                    "ws_connected": self._ws_connected, "subscribed": len(self._ws_subscribed),
                    "watched": [f"{i} {e}" for i, e in self.watch],
                    "last_tick_age": round(time.time() - self.last_tick_at, 1) if self.last_tick_at else None,
                    "errors": self.errors[-5:]}
