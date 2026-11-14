"""
mcx_terminal/nolive.py — the LIVE book of a desk that has no live orders yet (BTC desk, owner 15-Nov-2026:
paper first, live Delta orders later).

It has the live book's shape (the engine, risk guard and auto stop-loss talk to it like to live.LiveBook) but
never talks to a broker: it holds nothing, every order is refused, and arming LIVE is refused in the engine.
"""
import threading

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.paper import PaperBook

NO_LIVE = f"{C.DESK} desk is PAPER only - live {C.BROKER_LABEL} orders are not built yet"


class NoLiveBook(PaperBook):
    def __init__(self, universe, quotes, on_change=None, settings=None):
        super().__init__(universe, quotes, on_change=on_change, path=C.DATA / f"live_book_{C.day()}.json",
                         settings=settings)
        self.broker = {"ok": True, "msg": "paper only", "at": None}
        self.pos_sync = {"ok": True, "msg": "paper only", "at": None}
        self.own = True
        self.mismatch: list = []
        self.notify = None
        self.on_push_other = None
        self.sl_ids = lambda: set()
        self._poll_kick = threading.Event()

    # ── nothing is ever sent ──────────────────────────────────────────────────
    def place(self, *a, **kw) -> dict:
        return {"ok": False, "msg": NO_LIVE}

    def basket(self, *a, **kw) -> dict:
        return {"ok": False, "msg": NO_LIVE}

    def move(self, *a, **kw) -> dict:
        return {"ok": False, "msg": NO_LIVE}

    def _call(self, *a, **kw):
        raise RuntimeError(NO_LIVE)

    def _rows(self) -> list:
        return []

    # ── live-book hooks the engine calls ────────────────────────────────────
    def poll(self) -> bool:
        return False

    def sync_positions(self, raise_errors: bool = False) -> bool:
        return False

    def positions_fresh(self) -> bool:
        return True

    def on_push(self, _r):
        pass

    def on_push_position(self, _d):
        pass

    def housekeep(self):
        pass

    def working(self) -> int:
        return 0

    def open_positions(self) -> int:
        return 0

    def open_entry(self, token):
        return None

    def external_fill(self, *a, **kw):
        return False
