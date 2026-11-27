"""Cinematic 4K landscape scenes — Nifty part (27-Nov-2026, live Kotak D trade).
Real data only: 5-min candles of NIFTY26D0825800CE from the engine STATE file,
live fills from 2026-11-27_kotak_d.json, live MTM (kd_gross) from 2026-11-27_mtm.jsonl.

  python nifty_scenes.py preview 3.0 6.2 9.5      -> PNG stills (full 4K) in preview/
  python nifty_scenes.py video out.mp4            -> H.264 4K 24 fps master
"""
import sys, os, json, math
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pylib"))
import numpy as np
import cairo
from PIL import Image, ImageFilter

W, H, FPS = 3840, 2160, 24
BAR = 276                                   # 2.39:1 letterbox
RAW = os.path.join(HERE, "raw")
rng = np.random.default_rng(11)

# ------------------------------------------------------------------ data
state = json.load(open(os.path.join(RAW, "2026-11-27_STATE.json"), encoding="utf-8"))
C = state["panels"][0]["candles"]                       # 75 x 5-min, 09:15..15:25
assert state["panels"][0]["symbol"] == "NIFTY26D0825800CE"
book = json.load(open(os.path.join(RAW, "2026-11-27_kotak_d.json"), encoding="utf-8"))
ENTRY = next(f for f in book["fills"] if f["leg"] == "entry")
EXIT = next(f for f in book["fills"] if f["leg"] == "exit")
LOTS = ENTRY["qty"] // 65
def hms(s): h, m, x = map(int, s.split(":")); return h * 3600 + m * 60 + x
def cidx(sec):   # fractional candle index for an IST second-of-day
    return (sec - hms("09:15:00")) / 300.0
EX_IN, EX_OUT = cidx(hms(ENTRY["time"])), cidx(hms(EXIT["time"]))
P_IN, P_OUT = ENTRY["price"], EXIT["price"]
GROSS = round((P_IN - P_OUT) * ENTRY["qty"])

mt = []
for line in open(os.path.join(RAW, "2026-11-27_mtm.jsonl"), encoding="utf-8"):
    r = json.loads(line)
    if "09:55:00" <= r["t"] <= "12:53:00":
        mt.append((hms(r["t"]), r["kd_gross"]))
MT_T = np.array([a for a, _ in mt], float); MT_V = np.array([b for _, b in mt], float)
assert abs(MT_V[-1] - GROSS) < 1, (MT_V[-1], GROSS)

# ------------------------------------------------------------------ helpers
def ease(x): x = min(max(x, 0.0), 1.0); return x * x * (3 - 2 * x)
def ease_out(x): x = min(max(x, 0.0), 1.0); return 1 - (1 - x) ** 3
def lerp(a, b, t): return a + (b - a) * t
def seg(t, a, b): return min(max((t - a) / (b - a), 0.0), 1.0)
def hexc(h, a=1.0): h = h.lstrip("#"); return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)

UP, DN, AMBER, GREEN, WHITE = "#d8eef2", "#e2566e", "#ffb547", "#3ee08f", "#ffffff"
FONT = "Segoe UI"

def inr(n):
    s = f"{abs(int(round(n)))}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]; parts = []
        while len(head) > 2: parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("−" if n < 0 else "+") + "₹" + s

def text(cr, s, x, y, size, rgba=(1, 1, 1, 1), spacing=0.0, anchor="l", weight=cairo.FONT_WEIGHT_NORMAL):
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, weight); cr.set_font_size(size)
    widths = [cr.text_extents(ch).x_advance for ch in s]
    total = sum(widths) + spacing * (len(s) - 1)
    x0 = x - total if anchor == "r" else (x - total / 2 if anchor == "c" else x)
    cr.set_source_rgba(*rgba)
    for ch, w in zip(s, widths):
        cr.move_to(x0, y); cr.show_text(ch); x0 += w + spacing

# ------------------------------------------------------------------ post FX (precomputed)
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
VIG = np.clip(1.08 - 0.55 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2), 0.25, 1)[..., None]
del yy, xx
GRAIN = [rng.normal(0, 5.0, (H // 2, W // 2)).astype(np.float32) for _ in range(6)]

def surface_rgb(surf):
    a = np.ndarray((H, surf.get_stride() // 4, 4), np.uint8, surf.get_data())[:, :W]
    return a[:, :, 2::-1].astype(np.float32), a[:, :, 3:4].astype(np.float32) / 255

def post(scene_surf, text_surf, k, fade=1.0):
    img, _ = surface_rgb(scene_surf)
    # bloom: bright parts, blurred at 1/4 res
    small = Image.fromarray(img.astype(np.uint8)).resize((W // 4, H // 4), Image.BILINEAR)
    sm = np.asarray(small).astype(np.float32); sm = np.clip(sm - 70, 0, 255) * 1.6
    glow = Image.fromarray(sm.astype(np.uint8)).filter(ImageFilter.GaussianBlur(14)).resize((W, H), Image.BILINEAR)
    img = img + np.asarray(glow).astype(np.float32) * 0.65
    # grade: cool shadows, warm highlights
    n = img / 255
    img = 255 * np.clip(n + np.array([0.0, 0.018, 0.03]) * (1 - n) ** 3 + np.array([0.02, 0.008, -0.01]) * n ** 2, 0, 1)
    img *= VIG
    g = GRAIN[k % len(GRAIN)]; img += np.repeat(np.repeat(g, 2, 0), 2, 1)[..., None]
    trgb, ta = surface_rgb(text_surf)
    img = img * (1 - ta) + trgb * ta
    img *= fade
    img[:BAR] = 0; img[H - BAR:] = 0
    return np.clip(img, 0, 255).astype(np.uint8)

def new_surfaces():
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H); t = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    return s, cairo.Context(s), t, cairo.Context(t)

def background(cr, warm=0.0):
    g = cairo.RadialGradient(W * 0.55, H * 0.45, 100, W * 0.55, H * 0.5, W * 0.75)
    g.add_color_stop_rgb(0, 0.045 + warm * 0.02, 0.07, 0.09)
    g.add_color_stop_rgb(1, 0.008, 0.012, 0.018)
    cr.set_source(g); cr.paint()

# plot area (inside letterbox)
PX0, PX1, PY0, PY1 = 260, W - 420, BAR + 230, H - BAR - 230

class Cam:
    def __init__(s, x0, x1, y0, y1): s.x0, s.x1, s.y0, s.y1 = x0, x1, y0, y1
    def X(s, v): return PX0 + (v - s.x0) / (s.x1 - s.x0) * (PX1 - PX0)
    def Y(s, p): return PY1 - (p - s.y0) / (s.y1 - s.y0) * (PY1 - PY0)

# ------------------------------------------------------------------ scene 0: opener
def opener_frame(t, dur):
    s, cr, ts, tc = new_surfaces(); cr.set_source_rgb(0, 0, 0); cr.paint()
    a = ease(seg(t, 0.3, 1.2)) * (1 - ease(seg(t, dur - 0.6, dur)))
    text(tc, "F R I D A Y   ·   2 7   N O V   2 0 2 6", W / 2, H / 2 + 14, 46, (1, 1, 1, 0.85 * a), anchor="c")
    return s, ts

# ------------------------------------------------------------------ scene 1: option chart
def visible_range(lo_i, hi_i):
    lo_i = max(0, int(math.floor(lo_i))); hi_i = min(len(C) - 1, int(math.ceil(hi_i)))
    seg_ = C[lo_i:hi_i + 1] or C[:1]
    return min(c["low"] for c in seg_), max(c["high"] for c in seg_)

FULL_LO, FULL_HI = visible_range(0, 74)
_yr = {}

def chart_cam(t):
    """head = candles revealed; camera follows head, then pulls back to the whole day."""
    head = lerp(0.6, EX_OUT + 0.9, ease(seg(t, 0.2, 6.0))) if t < 6.0 else lerp(EX_OUT + 0.9, 75, ease(seg(t, 6.0, 7.8)))
    span = 24.0
    x1_follow = head + span * 0.32; x0_follow = x1_follow - span
    pb = ease(seg(t, 5.9, 8.2))
    x0 = lerp(x0_follow, -1.2, pb); x1 = lerp(x1_follow, 76.0, pb)
    lo, hi = visible_range(max(0, x0), min(head, x1))
    lo, hi = min(lo, P_OUT), max(hi, P_IN)
    lo, hi = lerp(lo, FULL_LO, pb), lerp(hi, FULL_HI, pb)
    pad = (hi - lo) * 0.16
    # smooth the y-range so the camera never jitters
    key = round(t * FPS)
    prev = _yr.get(key - 1, (lo - pad, hi + pad))
    k = 0.18
    y0 = prev[0] + (lo - pad - prev[0]) * k; y1 = prev[1] + (hi + pad - prev[1]) * k
    _yr[key] = (y0, y1)
    # slow push-in on top of everything
    push = 1 + 0.035 * t / 9
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return head, Cam(cx - (cx - x0) / push, cx + (x1 - cx) / push, cy - (cy - y0) / push, cy + (y1 - cy) / push)

def chart_frame(t, dur):
    s, cr, ts, tc = new_surfaces(); background(cr)
    head, cam = chart_cam(t)
    cr.rectangle(0, BAR, W, H - 2 * BAR); cr.clip()
    # faint horizontal price grid
    step = 10 if (cam.y1 - cam.y0) < 140 else 25
    p = math.ceil(cam.y0 / step) * step
    while p < cam.y1:
        y = cam.Y(p); cr.set_source_rgba(1, 1, 1, 0.035); cr.set_line_width(2); cr.move_to(0, y); cr.line_to(W, y); cr.stroke()
        text(cr, f"{p:.0f}", W - 150, y + 14, 34, (1, 1, 1, 0.22), anchor="r"); p += step
    cw = (cam.X(1) - cam.X(0)) * 0.56
    done = seg(t, 7.6, 8.6)   # end-state highlight of the captured move
    for i, c in enumerate(C):
        appear = min(max(head - i, 0), 1)
        if appear <= 0: break
        after_exit = i > EX_OUT + 0.5
        a = appear * (0.22 if after_exit else 1.0) * (1 - 0.35 * done * (not (EX_IN - 0.5 <= i <= EX_OUT + 0.5)))
        x = cam.X(i); col = UP if c["close"] >= c["open"] else DN
        cr.set_source_rgba(*hexc(col, 0.75 * a)); cr.set_line_width(max(2.0, cw * 0.09))
        cr.move_to(x, cam.Y(c["high"])); cr.line_to(x, cam.Y(c["low"])); cr.stroke()
        top, bot = cam.Y(max(c["open"], c["close"])), cam.Y(min(c["open"], c["close"]))
        cr.set_source_rgba(*hexc(col, 0.92 * a)); cr.rectangle(x - cw / 2, top, cw, max(bot - top, 3)); cr.fill()
    # entry / exit levels
    if head > EX_IN:
        x_in = cam.X(EX_IN); x_now = cam.X(min(head, EX_OUT))
        if done > 0:   # shaded captured zone
            g = cairo.LinearGradient(0, cam.Y(P_IN), 0, cam.Y(P_OUT))
            g.add_color_stop_rgba(0, *hexc(GREEN, 0.0)); g.add_color_stop_rgba(1, *hexc(GREEN, 0.20 * done))
            cr.set_source(g); cr.rectangle(x_in, cam.Y(P_IN), cam.X(EX_OUT) - x_in, cam.Y(P_OUT) - cam.Y(P_IN)); cr.fill()
        cr.set_source_rgba(*hexc(AMBER, 0.95)); cr.set_line_width(5); cr.set_dash([22, 14])
        cr.move_to(x_in, cam.Y(P_IN)); cr.line_to(x_now, cam.Y(P_IN)); cr.stroke(); cr.set_dash([])
        pulse = 1 + 0.5 * math.exp(-max(0, (head - EX_IN)) * 1.4)
        for r, al in ((44 * pulse, 0.18), (26, 0.45), (12, 1.0)):
            cr.set_source_rgba(*hexc(AMBER, al)); cr.arc(x_in, cam.Y(P_IN), r, 0, 2 * math.pi); cr.fill()
        la = ease(seg(head, EX_IN + 0.3, EX_IN + 1.6)) * (1 - 0.0 * done)
        text(tc, f"SELL   {P_IN:.2f}   ·   {ENTRY['time'][:5]}", x_in + 60, cam.Y(P_IN) - 52, 44, (1, 1, 1, 0.92 * la), spacing=3)
    if head > EX_OUT:
        x_out = cam.X(EX_OUT)
        cr.set_source_rgba(*hexc(GREEN, 0.95)); cr.set_line_width(5); cr.set_dash([22, 14])
        cr.move_to(cam.X(EX_IN), cam.Y(P_OUT)); cr.line_to(x_out, cam.Y(P_OUT)); cr.stroke(); cr.set_dash([])
        pulse = 1 + 0.5 * math.exp(-max(0, (head - EX_OUT)) * 1.2)
        for r, al in ((44 * pulse, 0.18), (26, 0.45), (12, 1.0)):
            cr.set_source_rgba(*hexc(GREEN, al)); cr.arc(x_out, cam.Y(P_OUT), r, 0, 2 * math.pi); cr.fill()
        la = ease(seg(head, EX_OUT + 0.2, EX_OUT + 1.4))
        text(tc, f"BUY   {P_OUT:.2f}   ·   {EXIT['time'][:5]}", x_out + 60, cam.Y(P_OUT) + 92, 44, (1, 1, 1, 0.92 * la), spacing=3)
    # captions (small, white)
    ca = ease(seg(t, 0.3, 1.3))
    text(tc, "NIFTY   25800 CE   ·   08 DEC", 300, BAR + 120, 40, (1, 1, 1, 0.80 * ca), spacing=6)
    text(tc, "5 - MIN", 300, BAR + 175, 32, (1, 1, 1, 0.6 * ca), spacing=6)
    if done > 0:
        text(tc, f"{P_IN - P_OUT:.2f}  POINTS   ·   {LOTS} LOTS   ·   LIVE", W / 2, H - BAR - 90, 42, (1, 1, 1, 0.9 * ease(done)), spacing=6, anchor="c")
    # fade in/out
    fade = ease(seg(t, 0, 0.6)) * (1 - ease(seg(t, dur - 0.5, dur)))
    return s, ts, fade

# ------------------------------------------------------------------ scene 2: live MTM
T0, T1 = MT_T[0], MT_T[-1]
def mtm_frame(t, dur):
    s, cr, ts, tc = new_surfaces(); background(cr, warm=0.4)
    draw_end = 5.6
    hp = ease(seg(t, 0.3, draw_end))
    head_t = lerp(T0, T1, hp)
    k = max(2, int(np.searchsorted(MT_T, head_t)))
    tt, vv = MT_T[:k], MT_V[:k]
    # camera: follow the head on a 70-min window, then pull back
    pb = ease(seg(t, draw_end - 0.6, draw_end + 1.2))
    win = 70 * 60
    fx1 = head_t + win * 0.25; fx0 = fx1 - win
    x0, x1 = lerp(fx0, T0 - 300, pb), lerp(fx1, T1 + 300, pb)
    lo, hi = min(MT_V.min(), 0), MT_V.max()
    vis = MT_V[(MT_T >= x0) & (MT_T <= head_t)] if pb < 1 else MT_V
    if len(vis) < 2: vis = MT_V[:2]
    flo, fhi = min(vis.min(), 0) - 1500, max(vis.max(), 1000) + 2500
    y0, y1 = lerp(flo, lo - 2500, pb), lerp(fhi, hi + 3500, pb)
    push = 1 + 0.03 * t / dur; cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cam = Cam(cx - (cx - x0) / push, cx + (x1 - cx) / push, cy - (cy - y0) / push, cy + (y1 - cy) / push)
    cr.rectangle(0, BAR, W, H - 2 * BAR); cr.clip()
    # zero line + faint grid
    yz = cam.Y(0)
    cr.set_source_rgba(1, 1, 1, 0.18); cr.set_line_width(2); cr.set_dash([10, 12]); cr.move_to(0, yz); cr.line_to(W, yz); cr.stroke(); cr.set_dash([])
    text(cr, "0", W - 150, yz + 14, 32, (1, 1, 1, 0.3), anchor="r")
    xs = cam.X(tt); ys = cam.Y(vv)
    # area fill
    def path():
        cr.move_to(xs[0], ys[0])
        for a, b in zip(xs[1:], ys[1:]): cr.line_to(a, b)
    for clip_above, col in ((True, GREEN), (False, DN)):
        cr.save()
        if clip_above: cr.rectangle(0, 0, W, yz)
        else: cr.rectangle(0, yz, W, H)
        cr.clip()
        path(); cr.line_to(xs[-1], yz); cr.line_to(xs[0], yz); cr.close_path()
        g = cairo.LinearGradient(0, cam.Y(hi), 0, yz) if clip_above else cairo.LinearGradient(0, cam.Y(lo), 0, yz)
        g.add_color_stop_rgba(0, *hexc(col, 0.30)); g.add_color_stop_rgba(1, *hexc(col, 0.0))
        cr.set_source(g); cr.fill()
        path(); cr.set_source_rgba(*hexc(col, 1)); cr.set_line_width(6); cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.stroke()
        cr.restore()
    # head
    hx, hy, hv = xs[-1], ys[-1], vv[-1]
    col = GREEN if hv >= 0 else DN
    for r, al in ((48, 0.16), (26, 0.5), (12, 1.0)):
        cr.set_source_rgba(*hexc(col, al)); cr.arc(hx, hy, r, 0, 2 * math.pi); cr.fill()
    clock = int(head_t); hh, mm = clock // 3600, clock % 3600 // 60
    la = 1 - ease(seg(t, draw_end, draw_end + 0.6))
    text(tc, inr(hv), hx + 60, hy - 40, 52, (1, 1, 1, 0.95 * la), spacing=2)
    text(tc, f"{hh:02d}:{mm:02d}", hx + 60, hy + 30, 36, (1, 1, 1, 0.7 * la), spacing=4)
    # captions
    ca = ease(seg(t, 0.3, 1.3))
    text(tc, "MTM   ·   LIVE   ·   KOTAK", 300, BAR + 120, 40, (1, 1, 1, 0.80 * ca), spacing=6)
    text(tc, f"NIFTY   ·   {LOTS} LOTS", 300, BAR + 175, 32, (1, 1, 1, 0.6 * ca), spacing=6)
    fa = ease(seg(t, draw_end + 0.6, draw_end + 1.6))
    if fa > 0:
        text(tc, inr(GROSS), W / 2, H - BAR - 120, 96, (1, 1, 1, fa), spacing=4, anchor="c", weight=cairo.FONT_WEIGHT_BOLD)
        text(tc, "NIFTY   ·   27 NOV", W / 2, H - BAR - 50, 36, (1, 1, 1, 0.7 * fa), spacing=8, anchor="c")
    fade = ease(seg(t, 0, 0.6)) * (1 - ease(seg(t, dur - 0.6, dur)))
    return s, ts, fade

# ------------------------------------------------------------------ timeline
SCENES = [("opener", opener_frame, 2.6), ("chart", chart_frame, 9.6), ("mtm", mtm_frame, 8.6)]

def frame_at(gt, k):
    acc = 0
    for name, fn, dur in SCENES:
        if gt < acc + dur:
            out = fn(gt - acc, dur)
            s, ts = out[0], out[1]; fade = out[2] if len(out) > 2 else 1.0
            return post(s, ts, k, fade)
        acc += dur
    raise ValueError(gt)

TOTAL = sum(d for _, _, d in SCENES)

def main():
    mode = sys.argv[1]
    if mode == "preview":
        os.makedirs(os.path.join(HERE, "preview"), exist_ok=True)
        # walk every frame up to each requested time so the camera smoothing is exact
        want = sorted(float(x) for x in sys.argv[2:])
        n = int(max(want) * FPS) + 1
        targets = {int(w * FPS): w for w in want}
        for k in range(n):
            gt = k / FPS
            if k in targets:
                Image.fromarray(frame_at(gt, k)).save(os.path.join(HERE, "preview", f"t{targets[k]:05.2f}.png")); print("still", targets[k], flush=True)
            else:   # advance camera state only
                acc = 0
                for name, fn, dur in SCENES:
                    if gt < acc + dur:
                        if name == "chart": chart_cam(gt - acc)
                        break
                    acc += dur
    else:
        import imageio_ffmpeg
        out = sys.argv[2]
        w = imageio_ffmpeg.write_frames(out, (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p", quality=None,
                                        output_params=["-crf", "12", "-preset", "slow", "-profile:v", "high", "-tune", "film",
                                                       "-movflags", "+faststart", "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709"],
                                        macro_block_size=8)
        w.send(None)
        n = int(TOTAL * FPS)
        for k in range(n):
            w.send(frame_at(k / FPS, k).tobytes())
            if k % 24 == 0: print(f"{k}/{n}", flush=True)
        w.close(); print("WROTE", out, TOTAL, "s")

if __name__ == "__main__":
    main()
