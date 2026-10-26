"""
mcx_terminal/bench_recorder.py — does the day recorder slow ticks or orders? Measured, not assumed.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.bench_recorder

1. Engine.on_ticks (the real code path every tick takes) with the recorder ON vs OFF, on realistic
   batches (as Kotak sends them), with the recorder's background writer running during the ON run.
2. The longest time the background writer holds Python's lock while packing a 5-second chunk at a
   heavy tick rate (what could, at worst, delay one tick or one order by that much).
"""
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from live_trading_options.mcx_terminal import recorder as R
from live_trading_options.mcx_terminal.server import Engine

sys.stdout.reconfigure(encoding="utf-8")


class NullRec:
    def tick(self, row):
        pass


def make_batches(n_batches, per_batch, toks):
    out = []
    for _ in range(n_batches):
        b = []
        for _ in range(per_batch):
            px = random.uniform(100, 900)
            b.append((random.choice(toks), px, px - .5, px + .5, 3, 2, random.randint(1, 90000), 500, px, 1,
                      1_792_000_000, 1.5, None))
        out.append(b)
    return out


def run(engine, batches):
    per = []
    for b in batches:
        t = time.perf_counter_ns()
        engine.on_ticks(b)
        per.append((time.perf_counter_ns() - t) / len(b))
    return per


def main():
    random.seed(1)
    toks = list(range(500000, 501200))
    batches = make_batches(4000, 25, toks)            # 100k ticks, Kotak-sized batches
    e = Engine()
    e.book_tokens = set()
    tmp = Path(tempfile.mkdtemp())

    e.rec = NullRec()
    run(e, batches[:200])                             # warm-up
    off = run(e, batches)

    rec = R.DayRecorder(day="2099-01-01", folder=tmp)
    rec.active = True                                 # write to disk too, like an active trading day
    e.rec = rec
    run(e, batches[:200])
    on = run(e, batches)
    rec.flush()

    def us(x):
        return f"{x / 1000:.3f} µs"
    print("per-tick cost of Engine.on_ticks (100,000 ticks):")
    print(f"  recorder OFF  median {us(statistics.median(off))}  p99 {us(sorted(off)[int(len(off) * .99)])}")
    print(f"  recorder ON   median {us(statistics.median(on))}  p99 {us(sorted(on)[int(len(on) * .99)])}")
    print(f"  difference    median {us(statistics.median(on) - statistics.median(off))} per tick")

    # worst case for the tick/order path: a probe thread that wants the CPU every 1 ms (like the
    # feed thread waiting for Kotak). How late does it get to run while the recorder saves heavy
    # 5-second chunks back to back (7,500 ticks each = ~5x the real MCX rate), vs recorder idle?
    import threading
    sys.setswitchinterval(0.0005)                     # the engine sets the same in server.main()

    def probe(seconds):
        """'Tick arrives' -> how long until the waiting thread is running (ms). Same mechanism as the
        feed thread waking on Kotak data: it has to get the CPU + Python's lock back."""
        ev, box, lat = threading.Event(), [0.0], []
        stop = threading.Event()

        def waiter():
            while not stop.is_set():
                if ev.wait(0.1):
                    lat.append((time.perf_counter() - box[0]) * 1000)
                    ev.clear()
        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            time.sleep(random.uniform(0.002, 0.004))
            box[0] = time.perf_counter()
            ev.set()
        stop.set()
        th.join()
        return lat

    def heavy_flushes(stop):
        while not stop.is_set():
            for i in range(7500):
                rec.ticks.append((1_792_000_000_000 + i, random.choice(toks), 500.0, 499.5, 500.5, 3, 2,
                                  1000 + i, 500, 500.0, 1, 1_792_000_000, 1.5))
            rec.flush()

    idle = probe(3)
    stop = threading.Event()
    w = threading.Thread(target=heavy_flushes, args=(stop,), daemon=True)
    w.start()
    busy = probe(3)
    stop.set()
    w.join()
    q = lambda xs, f: sorted(xs)[int(len(xs) * f)]
    print("'tick arrived' -> engine thread running, in ms (lower = faster):")
    print(f"  recorder idle             median {statistics.median(idle):.3f}  p99 {q(idle, .99):.3f}  max {max(idle):.3f}")
    print(f"  recorder saving non-stop  median {statistics.median(busy):.3f}  p99 {q(busy, .99):.3f}  max {max(busy):.3f}")
    print(f"  longest single pack-and-compress step inside the writer: {rec.stats['pack_ms_max']} ms (zlib part runs outside the lock)")
    print(f"recorded {rec.stats['ticks']} ticks -> {rec.stats['bytes'] / 1e6:.2f} MB on disk "
          f"({rec.stats['bytes'] / max(rec.stats['ticks'], 1):.1f} bytes/tick)")


if __name__ == "__main__":
    main()
