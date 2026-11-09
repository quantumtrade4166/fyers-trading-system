"""cepe_imbalance/test_days.py — past-day read-back (EOD P&L, counts, downsampling) on tiny fake files."""
import json

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import days


def _write(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    b = tmp_path / "books" / "2026-11-02"
    h = tmp_path / "history" / "2026-11-02"
    b.mkdir(parents=True)
    h.mkdir(parents=True)
    pos = {"k1": {"key": "k1", "symbol": "X", "kind": "CE", "strike": 9200.0, "expiry": "2026-11-17",
                  "unit_value": 100, "lots": 0, "avg": 0.0, "realized": -10810.0}}
    (b / "CRUDEOIL.json").write_text(json.dumps({"positions": pos}), encoding="utf-8")
    (b / "CRUDEOIL_trades.jsonl").write_text("\n".join(json.dumps({"time": f"20:50:{i:02d}.000"}) for i in range(14)),
                                            encoding="utf-8")
    ev = [{"event": "adjust", "action": "entry"}, {"event": "adjust", "action": "ATM rule CE"},
          {"event": "adjust", "action": "A (CE heavy +16.0%)"}, {"event": "atm_change"}]
    (b / "CRUDEOIL_events.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    rows = [{"t": 1000 + i * 0.5, "mtm": float(i), "imb": i % 7 - 3} for i in range(10000)]
    (h / "CRUDEOIL.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + '\n{"t": 9', encoding="utf-8")
    return rows


def test_summary_and_days(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch)
    assert days.list_days() == ["2026-11-02"]
    s = days.summary("2026-11-02", "CRUDEOIL")
    assert s["pnl"] == -10810.0 and s["fills"] == 14
    assert (s["adjustments"], s["atm_rule"], s["a"], s["b"]) == (2, 1, 1, 0)
    assert s["mtm_high"] == 9999.0 and s["imb_min"] == -3
    assert days.summary("2026-11-02", "GOLD")["traded"] is False


def test_downsample_keeps_ends(tmp_path, monkeypatch):
    rows = _write(tmp_path, monkeypatch)
    got = days.history("2026-11-02", "CRUDEOIL")
    assert len(got) == 10000                                           # broken last line skipped
    d = days.downsample(got, 500)
    assert len(d) <= 502 and d[0] == rows[0] and d[-1] == rows[-1]


def test_summary_uses_in_memory_history_for_today():
    rows = [{"t": 1, "mtm": -50.0, "imb": 3.0}, {"t": 2, "mtm": 80.0, "imb": -9.0}]
    s = days.summary("2099-01-01", "GOLD", hist=rows)        # no files for that date
    assert s["mtm_high"] == 80.0 and s["mtm_low"] == -50.0 and s["imb_min"] == -9.0


def test_pnl_series_cumulative_and_drawdown(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch)                                       # 2026-11-02: -10810
    b = tmp_path / "books" / "2026-11-03"
    b.mkdir(parents=True)
    pos = {"k": {"kind": "PE", "strike": 9000.0, "expiry": "2026-11-17", "lots": 0, "realized": 4000.0}}
    (b / "CRUDEOIL.json").write_text(json.dumps({"positions": pos}), encoding="utf-8")
    (b / "CRUDEOIL_trades.jsonl").write_text(json.dumps({"time": "20:00:00.000"}), encoding="utf-8")
    got = days.pnl_series("CRUDEOIL", today="2026-11-04", today_pnl=9000.0)
    assert [x["date"] for x in got] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    assert [x["cum"] for x in got] == [-10810.0, -6810.0, 2190.0]
    assert [x["dd"] for x in got] == [-10810.0, -6810.0, 0.0]
    assert days.pnl_series("GOLD") == []                                # never traded -> no bars
