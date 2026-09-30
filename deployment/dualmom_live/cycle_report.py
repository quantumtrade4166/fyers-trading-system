"""
Per-cycle performance record for a DualMom account - the client-facing statement.

WHAT A CYCLE IS
    DualMom is either invested or in cash: the Nifty-vs-100MA filter takes the
    whole book to cash on an OUT month. So a CYCLE is one uninterrupted spell of
    being invested - from the first buy that lifts the book off zero, to the sell
    that flattens it again. Sept-2026 IN -> Oct-2026 OUT is one cycle.

    That is the unit a client asks about: what did this round trip make or lose?
    Month boundaries INSIDE a cycle are rebalances, not new cycles.

WHY FIFO
    Realised P&L is matched lot by lot, oldest first - the convention Indian
    capital-gains uses on delivery trades, so these numbers reconcile with the
    tax statement instead of approximating it with an average cost.

WHAT IS AN ESTIMATE
    Charges come from each fill's stored `charges_est`, costed with the rate-card
    version recorded ON that fill. Kotak brokerage is still unverified against a
    contract note (config.CHARGES_RATE_CARD.brokerage_verified), so
    `charges_verified` is surfaced at the top of every report rather than buried.
    Nothing here presents an estimate as a settled number.

    Likewise, a position still held with no live mark reports unrealised as None,
    and the cycle total is WITHHELD - an unknown is not a profit of nil.

ACCOUNT-AGNOSTIC
    Both accounts keep the identical ledger format, so this takes the ledger and
    config modules as arguments and serves Kotak and Kite alike.
"""

import csv
import io
import json
from datetime import date, datetime

# -- helpers -----------------------------------------------------------------


def _d(x):
    """Date part of an ISO-ish timestamp, or None."""
    s = str(x or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _charges(fill) -> float:
    return float((fill.get("charges_est") or {}).get("total", 0.0) or 0.0)


def _sorted_fills(fills):
    return sorted(fills, key=lambda f: (f.get("exchange_time") or "",
                                        str(f.get("exchange_fill_id") or "")))


# -- cycle detection ---------------------------------------------------------


def split_cycles(fills) -> list:
    """Cut the fill stream into invested spells.

    Returns [{start, end, open, fills}]; `end` is None while the cycle is still
    open. Book size is tracked across ALL symbols, so a cycle closes only when
    every position is flat - not when one name is sold.
    """
    out, cur, held = [], None, {}
    for f in _sorted_fills(fills):
        sym, q = f["symbol"], int(f["qty"])
        was_flat = not any(v > 0 for v in held.values())
        held[sym] = held.get(sym, 0) + (q if f["side"] == "BUY" else -q)
        if was_flat and held.get(sym, 0) > 0 and cur is None:
            cur = {"start": _d(f.get("exchange_time")), "end": None,
                   "open": True, "fills": []}
        if cur is not None:
            cur["fills"].append(f)
            if not any(v > 0 for v in held.values()):
                cur["end"] = _d(f.get("exchange_time"))
                cur["open"] = False
                out.append(cur)
                cur, held = None, {}
    if cur is not None:
        out.append(cur)
    return out


# -- per-symbol P&L inside one cycle -----------------------------------------


def symbol_rows(cycle_fills, marks=None) -> list:
    """FIFO realised, plus unrealised at `marks` for anything still held.

    marks = {symbol: last_price}. A symbol still open with no mark reports
    unrealised None, never zero.
    """
    marks = marks or {}
    lots, rows = {}, {}
    for f in _sorted_fills(cycle_fills):
        s = f["symbol"]
        r = rows.setdefault(s, {
            "symbol": s, "trading_symbol": f.get("trading_symbol"),
            "buy_qty": 0, "buy_value": 0.0, "sell_qty": 0, "sell_value": 0.0,
            "realized": 0.0, "charges": 0.0, "first_buy": None, "last_sell": None,
            "open_qty": 0, "open_cost": 0.0,
        })
        q, px = int(f["qty"]), float(f["price"])
        r["charges"] += _charges(f)
        book = lots.setdefault(s, [])
        if f["side"] == "BUY":
            r["buy_qty"] += q
            r["buy_value"] += q * px
            r["first_buy"] = r["first_buy"] or _d(f.get("exchange_time"))
            book.append([q, px])
        else:
            r["sell_qty"] += q
            r["sell_value"] += q * px
            r["last_sell"] = _d(f.get("exchange_time"))
            rem = q
            while rem > 0 and book:
                lot = book[0]
                take = min(rem, lot[0])
                r["realized"] += take * (px - lot[1])
                lot[0] -= take
                rem -= take
                if lot[0] == 0:
                    book.pop(0)
    for s, r in rows.items():
        book = lots.get(s, [])
        r["open_qty"] = sum(l[0] for l in book)
        r["open_cost"] = sum(l[0] * l[1] for l in book)
        r["avg_buy"] = r["buy_value"] / r["buy_qty"] if r["buy_qty"] else 0.0
        r["avg_sell"] = r["sell_value"] / r["sell_qty"] if r["sell_qty"] else None
        mk = marks.get(s)
        if r["open_qty"] > 0:
            r["mark"] = mk
            r["unrealized"] = (r["open_qty"] * mk - r["open_cost"]) if mk else None
        else:
            r["mark"], r["unrealized"] = None, 0.0
        gross = r["realized"] + (r["unrealized"] or 0.0)
        r["gross_pnl"] = gross
        r["net_pnl"] = gross - r["charges"]
        base = r["buy_value"] or None
        r["return_pct"] = (r["net_pnl"] / base * 100) if base else None
        r["held_days"] = ((r["last_sell"] or date.today()) - r["first_buy"]).days \
            if r["first_buy"] else None
        r["pnl_incomplete"] = r["open_qty"] > 0 and mk is None
    return sorted(rows.values(), key=lambda x: -x["net_pnl"])


# -- the report --------------------------------------------------------------


def build(ledger_mod, config_mod, marks=None, benchmark=None) -> dict:
    """One report for one account.

    ledger_mod  dualmom_live.ledger or dualmom_kite.ledger
    config_mod  the matching config module
    marks       {symbol: ltp}, used for an open cycle
    benchmark   {"name", "start_close", "end_close"} - optional

    Delivery equity only. Anything else the shared account traded is excluded
    from the cycles and listed under `excluded_fills` - shown, never dropped.
    """
    from deployment.dualmom_live import ledger as _LK
    all_fills = ledger_mod._read("fills.jsonl")
    fills, foreign = _LK.split_foreign(all_fills)
    rate = getattr(config_mod, "CHARGES_RATE_CARD", {}) or {}
    out_cycles = []

    for i, c in enumerate(split_cycles(fills), 1):
        rows = symbol_rows(c["fills"], marks if c["open"] else None)
        if hasattr(config_mod, "contributed"):
            capital = config_mod.contributed(c["start"])
        else:
            capital = getattr(config_mod, "CAPITAL_BASE", 0)
        realized = sum(r["realized"] for r in rows)
        unreal = sum((r["unrealized"] or 0.0) for r in rows)
        charges = sum(r["charges"] for r in rows)
        deployed = sum(r["buy_value"] for r in rows)
        net = realized + unreal - charges
        incomplete = [r["symbol"] for r in rows if r["pnl_incomplete"]]
        winners = [r for r in rows if r["net_pnl"] > 0]
        losers = [r for r in rows if r["net_pnl"] < 0]
        for r in rows:
            r["contribution_pct"] = (r["net_pnl"] / capital * 100) if capital else None

        bm = None
        if benchmark and benchmark.get("start_close") and benchmark.get("end_close"):
            bm = {"name": benchmark.get("name"),
                  "start_close": benchmark["start_close"],
                  "end_close": benchmark["end_close"],
                  "return_pct": round(
                      (benchmark["end_close"] / benchmark["start_close"] - 1) * 100, 3)}

        out_cycles.append({
            "cycle": i,
            "start": str(c["start"]),
            "end": str(c["end"]) if c["end"] else None,
            "open": c["open"],
            "days": ((c["end"] or date.today()) - c["start"]).days if c["start"] else None,
            "capital_at_start": capital,
            "deployed": round(deployed, 2),
            "deployed_pct_of_capital": round(deployed / capital * 100, 2) if capital else None,
            "realized": round(realized, 2),
            "unrealized": round(unreal, 2) if not incomplete else None,
            "charges_est": round(charges, 2),
            "net_pnl": round(net, 2) if not incomplete else None,
            "return_pct": round(net / capital * 100, 3) if capital and not incomplete else None,
            "positions": len(rows),
            "fills": len(c["fills"]),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate_pct": round(len(winners) / len(rows) * 100, 1) if rows else None,
            "best": rows[0]["symbol"] if rows else None,
            "best_pnl": round(rows[0]["net_pnl"], 2) if rows else None,
            "worst": rows[-1]["symbol"] if rows else None,
            "worst_pnl": round(rows[-1]["net_pnl"], 2) if rows else None,
            "benchmark": bm,
            "pnl_incomplete_for": incomplete,
            "rows": rows,
        })

    return {
        "account": getattr(config_mod, "CLIENT_ACCOUNT", "?"),
        "broker": getattr(config_mod, "BROKER", "?"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "capital_total": getattr(config_mod, "CAPITAL_BASE", None),
        "capital_flows": getattr(config_mod, "CAPITAL_FLOWS", None),
        "charges_rate_card": rate.get("version"),
        "charges_verified": bool(rate.get("brokerage_verified")),
        "total_fills": len(all_fills),
        "equity_fills": len(fills),
        "excluded_fills": [
            {"symbol": f.get("symbol"), "trading_symbol": f.get("trading_symbol"),
             "segment": f.get("segment"), "product": f.get("product"),
             "side": f.get("side"), "qty": f.get("qty"), "price": f.get("price"),
             "exchange_time": f.get("exchange_time"), "client_tag": f.get("client_tag")}
            for f in foreign],
        "cycles": out_cycles,
    }


# -- rendering ---------------------------------------------------------------


def _rs(v, dp=2):
    return "-" if v is None else f"{v:,.{dp}f}"


def render_text(rep: dict) -> str:
    L = []
    A = L.append
    A("=" * 80)
    A(f"DualMom - {rep['account']} ({rep['broker']})")
    A(f"generated {rep['generated_at']}    fills on record: {rep['total_fills']}"
      f"  (delivery equity: {rep.get('equity_fills', rep['total_fills'])})")
    if not rep["charges_verified"]:
        A("NOTE: brokerage in the rate card is UNVERIFIED against a contract note -")
        A(f"      charges below are ESTIMATES (rate card {rep['charges_rate_card']}).")
    for x in rep.get("excluded_fills") or []:
        A(f"EXCLUDED (not DualMom delivery equity): {x['trading_symbol']} "
          f"{x['side']} {x['qty']} @ {x['price']}  {x['segment']}/{x['product']}  "
          f"{x['exchange_time']}")
    if rep.get("excluded_fills"):
        A("      Those fills stay in the ledger as a record of the account, but are")
        A("      not DualMom trades and are excluded from every figure below.")
    A("=" * 80)
    for c in rep["cycles"]:
        A("")
        A(f"CYCLE {c['cycle']} [{'OPEN' if c['open'] else 'CLOSED'}]  "
          f"{c['start']} -> {c['end'] or 'still invested'}   ({c['days']} days)")
        A("-" * 80)
        A(f"  capital at start   {_rs(c['capital_at_start'], 0):>16}")
        A(f"  deployed           {_rs(c['deployed']):>16}   "
          f"({_rs(c['deployed_pct_of_capital'])}% of capital)")
        A(f"  realised           {_rs(c['realized']):>16}")
        A(f"  unrealised         {_rs(c['unrealized']):>16}")
        A(f"  charges (est)      {_rs(c['charges_est']):>16}")
        A(f"  NET P&L            {_rs(c['net_pnl']):>16}   "
          f"({_rs(c['return_pct'], 3)}% on capital)")
        if c["benchmark"]:
            b = c["benchmark"]
            A(f"  {b['name']:<18} {_rs(b['return_pct'], 3):>15}%  over the same window")
            if c["return_pct"] is not None:
                A(f"  excess vs benchmark {_rs(c['return_pct'] - b['return_pct'], 3):>15}%")
        A(f"  positions {c['positions']}   fills {c['fills']}   "
          f"winners {c['winners']} / losers {c['losers']}   "
          f"win rate {_rs(c['win_rate_pct'], 1)}%")
        if c["pnl_incomplete_for"]:
            A(f"  !! no live mark for: {', '.join(c['pnl_incomplete_for'])}")
            A("     cycle totals withheld rather than reported as zero")
        A("")
        A(f"  {'symbol':<14}{'qty':>6}{'avg buy':>11}{'avg sell':>11}"
          f"{'net P&L':>13}{'ret %':>9}{'contrib %':>11}{'days':>6}")
        A("  " + "-" * 78)
        for r in c["rows"]:
            A(f"  {r['symbol']:<14}{r['buy_qty']:>6}{_rs(r['avg_buy']):>11}"
              f"{_rs(r['avg_sell']):>11}{_rs(r['net_pnl']):>13}"
              f"{_rs(r['return_pct']):>9}{_rs(r['contribution_pct'], 3):>11}"
              f"{(r['held_days'] if r['held_days'] is not None else '-'):>6}")
    return "\n".join(L)


def render_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["account", "cycle", "cycle_start", "cycle_end", "open", "symbol",
                "buy_qty", "avg_buy", "sell_qty", "avg_sell", "open_qty", "mark",
                "realized", "unrealized", "charges_est", "net_pnl", "return_pct",
                "contribution_pct", "held_days"])
    for c in rep["cycles"]:
        for r in c["rows"]:
            w.writerow([
                rep["account"], c["cycle"], c["start"], c["end"], c["open"],
                r["symbol"], r["buy_qty"], round(r["avg_buy"], 4), r["sell_qty"],
                round(r["avg_sell"], 4) if r["avg_sell"] else "",
                r["open_qty"], r["mark"] if r["mark"] else "",
                round(r["realized"], 2),
                round(r["unrealized"], 2) if r["unrealized"] is not None else "",
                round(r["charges"], 2), round(r["net_pnl"], 2),
                round(r["return_pct"], 3) if r["return_pct"] is not None else "",
                round(r["contribution_pct"], 4) if r["contribution_pct"] is not None else "",
                r["held_days"]])
    return buf.getvalue()


def save(rep: dict, out_dir, stem=None) -> dict:
    """Write .json / .txt / .csv side by side. Returns {ext: path}."""
    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or f"cycle_{rep['account'].replace(' ', '_')}_{date.today():%Y%m%d}"
    paths = {}
    for ext, data in (("json", json.dumps(rep, indent=2, default=str)),
                      ("txt", render_text(rep)),
                      ("csv", render_csv(rep))):
        p = out_dir / f"{stem}.{ext}"
        p.write_text(data, encoding="utf-8")
        paths[ext] = str(p)
    return paths
