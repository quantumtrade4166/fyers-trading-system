"""READ-ONLY timing: 8 order_report() calls with the SDK as-is, then 8 with fastrest installed."""
import statistics
import sys
import time

from live_trading_options.mcx_terminal import fastrest
from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")


def bench(c, label):
    ts = []
    for _ in range(8):
        t = time.perf_counter()
        c.order_report()
        ts.append((time.perf_counter() - t) * 1000)
    print(f"{label}: median {statistics.median(ts):.0f} ms · min {min(ts):.0f} · max {max(ts):.0f}")


if __name__ == "__main__":
    s = login()
    bench(s.client, "SDK as-is (new connection each call)")
    fastrest.install()
    bench(s.client, "keep-alive session")
    sys.stdout.flush()
    import os
    os._exit(0)
