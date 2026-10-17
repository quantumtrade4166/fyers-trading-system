"""
strategy_builder/store.py — saved strategies (name + legs), one JSON file.

Atomic write (tmp + replace) so a crash mid-save can never leave a half file, and
every save keeps the previous file as strategies.json.bak.
"""
import json
import threading
import time
import uuid

from live_trading_options.strategy_builder import config as C

_lock = threading.Lock()
LEG_KEYS = ("symbol", "index", "expiry", "strike", "type", "side", "lots", "price")


def _load() -> list:
    try:
        return json.loads(C.STRATEGIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _save(items: list):
    C.DATA.mkdir(parents=True, exist_ok=True)
    tmp = C.STRATEGIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    if C.STRATEGIES_FILE.exists():
        C.STRATEGIES_FILE.replace(C.STRATEGIES_FILE.with_suffix(".json.bak"))
    tmp.replace(C.STRATEGIES_FILE)


def clean_legs(legs) -> list:
    out = []
    for l in legs or []:
        leg = {k: l.get(k) for k in LEG_KEYS}
        if leg["side"] not in ("B", "S") or leg["type"] not in ("CE", "PE"):
            raise ValueError(f"bad leg: {l}")
        if leg["index"] not in C.INDICES:
            raise ValueError(f"bad index: {leg['index']}")
        leg["lots"] = int(leg["lots"])
        if not 1 <= leg["lots"] <= 100:
            raise ValueError("lots must be 1..100")
        leg["strike"] = float(leg["strike"])
        leg["price"] = float(leg["price"]) if leg["price"] is not None else None
        out.append(leg)
    return out


def list_all() -> list:
    with _lock:
        return _load()


def upsert(name: str, index: str, legs: list, sid: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    legs = clean_legs(legs)
    with _lock:
        items = _load()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for it in items:
            if it["id"] == sid:
                it.update(name=name, index=index, legs=legs, updated=now)
                _save(items)
                return it
        if any(it["name"].lower() == name.lower() and not it.get("archived") for it in items):
            raise ValueError(f"a strategy named '{name}' already exists")
        it = {"id": uuid.uuid4().hex[:10], "name": name, "index": index, "legs": legs,
              "created": now, "updated": now, "archived": False}
        items.append(it)
        _save(items)
        return it


def archive(sid: str, archived: bool = True) -> bool:
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == sid:
                it["archived"] = archived
                _save(items)
                return True
    return False
