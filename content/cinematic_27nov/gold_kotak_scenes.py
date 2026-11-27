"""Cinematic 4K scenes — Gold MTM time-lapse + Kotak positions panel (27-Nov-2026).
Gold: real live MTM from the MCX terminal (mtm_live_2026-11-27.json, Kotak P, live orders).
Kotak: real Kotak Neo positions screenshots, stitched (kotak/header.png + kotak/body.png).

  python gold_kotak_scenes.py preview 3 7 11 15
  python gold_kotak_scenes.py video out.mp4
"""
import sys, os, json, math, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "pylib"))
import numpy as np, cairo
from PIL import Image, ImageFilter, ImageEnhance
import nifty_scenes as N
from nifty_scenes import W, H, BAR, FPS, ease, seg, lerp, hexc, text, inr, post, new_surfaces, background

GOLD = "#f2c14e"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
m = json.load(open(os.path.join(N.RAW, "mtm_live_2026-11-27.json"), encoding="utf-8"))
G_T = np.array([p[0] for p in m]); G_V = np.array([p[1] for p in m], float)
def ist(t): return dt.datetime.fromtimestamp(t, IST)
G_END_LABEL = ist(G_T[-1]).strftime("%H:%M")
G_FINAL = G_V[-1]
_w = np.abs(np.diff(G_V, prepend=G_V[0])) + 6.0
G_PACE = np.cumsum(_w); G_PACE = (G_PACE - G_PACE[0]) / (G_PACE[-1] - G_PACE[0])
def pace_time(u):          # progress 0..1 -> timestamp, more screen time where MTM moves
    return float(np.interp(u, G_PACE, G_T))

# ------------------------------------------------------------------ gold MTM time-lapse
def gold_frame(t, dur):
    s, cr, ts, tc = new_surfaces(); background(cr, warm=1.0)
    draw_end = dur - 2.6
    hp = ease(seg(t, 0.4, draw_end)) ** 1.05
    head_t = pace_time(hp)
    k = max(2, int(np.searchsorted(G_T, head_t)))
    tt, vv = G_T[:k], G_V[:k]
    pb = ease(seg(t, draw_end - 0.8, draw_end + 1.0))
    win = 95 * 60
    fx1 = head_t + win * 0.22; fx0 = fx1 - win
    x0, x1 = lerp(fx0, G_T[0] - 400, pb), lerp(fx1, G_T[-1] + 400, pb)
    vis = G_V[(G_T >= x0) & (G_T <= head_t)]
    if len(vis) < 2: vis = G_V[:2]
    lo_all, hi_all = min(G_V.min(), 0), G_V.max()
    span_ = max(vis.max() - vis.min(), 9000)
    y0 = lerp(vis.min() - span_ * 0.35, lo_all - 5000, pb); y1 = lerp(vis.max() + span_ * 0.45, hi_all + 8000, pb)
    push = 1 + 0.03 * t / dur; cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cam = N.Cam(cx - (cx - x0) / push, cx + (x1 - cx) / push, cy - (cy - y0) / push, cy + (y1 - cy) / push)
    cr.rectangle(0, BAR, W, H - 2 * BAR); cr.clip()
    yz = cam.Y(0)
    cr.set_source_rgba(1, 1, 1, 0.16); cr.set_line_width(2); cr.set_dash([10, 12]); cr.move_to(0, yz); cr.line_to(W, yz); cr.stroke(); cr.set_dash([])
    # faint hour ticks
    hr = ist(x0).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while hr.timestamp() < x1:
        x = cam.X(hr.timestamp()); cr.set_source_rgba(1, 1, 1, 0.05); cr.move_to(x, BAR); cr.line_to(x, H - BAR); cr.stroke()
        text(cr, hr.strftime("%H:%M"), x + 14, H - BAR - 40, 30, (1, 1, 1, 0.28 * (1 - ease(seg(t, dur - 3.0, dur - 2.2)))), spacing=3); hr += dt.timedelta(hours=1)
    xs, ys = cam.X(tt.astype(float)), cam.Y(vv)
    def path():
        cr.move_to(xs[0], ys[0])
        for a, b in zip(xs[1:], ys[1:]): cr.line_to(a, b)
    for above, col in ((True, GOLD), (False, N.DN)):
        cr.save(); cr.rectangle(0, 0, W, yz) if above else cr.rectangle(0, yz, W, H); cr.clip()
        path(); cr.line_to(xs[-1], yz); cr.line_to(xs[0], yz); cr.close_path()
        g = cairo.LinearGradient(0, cam.Y(hi_all) if above else cam.Y(lo_all), 0, yz)
        g.add_color_stop_rgba(0, *hexc(col, 0.30)); g.add_color_stop_rgba(1, *hexc(col, 0.0)); cr.set_source(g); cr.fill()
        path(); cr.set_source_rgba(*hexc(col, 1)); cr.set_line_width(6); cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.stroke()
        cr.restore()
    hx, hy, hv = xs[-1], ys[-1], vv[-1]; col = GOLD if hv >= 0 else N.DN
    for r, al in ((50, 0.16), (26, 0.5), (12, 1.0)):
        cr.set_source_rgba(*hexc(col, al)); cr.arc(hx, hy, r, 0, 2 * math.pi); cr.fill()
    la = 1 - ease(seg(t, draw_end, draw_end + 0.5))
    text(tc, inr(hv), hx + 60, hy - 40, 52, (1, 1, 1, 0.95 * la), spacing=2)
    # racing clock, top right
    ca = ease(seg(t, 0.3, 1.3))
    text(tc, ist(head_t).strftime("%H:%M:%S"), W - 300, BAR + 125, 64, (1, 1, 1, 0.9 * ca * la + 0.0), spacing=6, anchor="r")
    text(tc, "IST", W - 300, BAR + 180, 30, (1, 1, 1, 0.5 * ca * la), spacing=6, anchor="r")
    text(tc, "GOLD   ·   MCX   ·   OPTIONS", 300, BAR + 120, 40, (1, 1, 1, 0.8 * ca), spacing=6)
    text(tc, "MTM   ·   LIVE   ·   KOTAK", 300, BAR + 175, 32, (1, 1, 1, 0.6 * ca), spacing=6)
    fa = ease(seg(t, draw_end + 0.5, draw_end + 1.5))
    if fa > 0:
        text(tc, inr(G_FINAL), W / 2, H - BAR - 120, 96, (1, 1, 1, fa), spacing=4, anchor="c", weight=cairo.FONT_WEIGHT_BOLD)
        text(tc, f"GOLD   ·   27 NOV   ·   AS OF {G_END_LABEL}", W / 2, H - BAR - 50, 36, (1, 1, 1, 0.7 * fa), spacing=8, anchor="c")
    fade = ease(seg(t, 0, 0.6)) * (1 - ease(seg(t, dur - 0.6, dur)))
    return s, ts, fade

# ------------------------------------------------------------------ Kotak panel
KH = Image.open(os.path.join(HERE, "kotak", "header.png")).convert("RGB")
KB = Image.open(os.path.join(HERE, "kotak", "body.png")).convert("RGB")
CROPW = 1140                                    # drop the scrollbar strip on the right
KH, KB = KH.crop((0, 0, CROPW, KH.height)), KB.crop((0, 0, CROPW, KB.height))
VIEW = 520                                      # visible body height (screenshot px)
PS = 2.05                                       # screenshot px -> 4K px
def find_coeffs(pa, pb):
    A = []
    for p1, p2 in zip(pa, pb):
        A.append([p1[0], p1[1], 1, 0, 0, 0, -p2[0] * p1[0], -p2[0] * p1[1]])
        A.append([0, 0, 0, p1[0], p1[1], 1, -p2[1] * p1[0], -p2[1] * p1[1]])
    return np.linalg.solve(np.array(A, float), np.array(pb, float).reshape(8))

def kotak_frame(t, dur):
    s, cr, ts, tc = new_surfaces(); background(cr)
    sp = ease(seg(t, 1.0, dur - 1.6))
    off = int(sp * (KB.height - VIEW))
    panel = Image.new("RGB", (CROPW, KH.height + VIEW))
    panel.paste(KH, (0, 0)); panel.paste(KB.crop((0, off, CROPW, off + VIEW)), (0, KH.height))
    pw, ph = int(CROPW * PS), int(panel.height * PS)
    big = panel.resize((pw, ph), Image.LANCZOS)
    big = ImageEnhance.Sharpness(big).enhance(1.6)
    big = ImageEnhance.Brightness(big).enhance(1.45)
    big = ImageEnhance.Contrast(big).enhance(1.12)
    # gentle 3D tilt that slowly straightens, plus a slow push-in
    tilt = lerp(0.075, 0.025, ease(t / dur)); z = lerp(0.96, 1.04, ease(t / dur))
    w2, h2 = pw * z, ph * z
    cx, cy = W / 2 + lerp(40, -20, t / dur), H / 2 + 10
    dst = [(cx - w2 / 2, cy - h2 / 2 + h2 * tilt * 0.5), (cx + w2 / 2, cy - h2 / 2),
           (cx + w2 / 2, cy + h2 / 2), (cx - w2 / 2, cy + h2 / 2 - h2 * tilt * 0.5)]
    src = [(0, 0), (pw, 0), (pw, ph), (0, ph)]
    coeffs = find_coeffs(dst, src)
    layer = big.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=(0, 0, 0))
    mask = Image.new("L", big.size, 255).transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=0)
    # depth of field: soften the far edges of the panel
    blur = layer.filter(ImageFilter.GaussianBlur(5))
    yy = np.linspace(0, 1, H)[:, None]; xx = np.linspace(0, 1, W)[None, :]
    dof = np.clip(np.maximum(np.abs(yy - 0.5) * 2 - 0.55, 0) * 2.2 + np.maximum(0.12 - xx, 0) * 5, 0, 1)[..., None]
    L = np.asarray(layer).astype(np.float32) * (1 - dof) + np.asarray(blur).astype(np.float32) * dof
    bg = np.ndarray((H, s.get_stride() // 4, 4), np.uint8, s.get_data())[:, :W]
    base = bg[:, :, 2::-1].astype(np.float32)
    # soft shadow + glow under the panel
    sh = mask.filter(ImageFilter.GaussianBlur(60)); sha = np.asarray(sh).astype(np.float32)[..., None] / 255
    base = base * (1 - 0.6 * sha)
    ma = np.asarray(mask).astype(np.float32)[..., None] / 255
    out = base * (1 - ma) + L * ma
    bg[:, :, 2::-1] = np.clip(out, 0, 255).astype(np.uint8); bg[:, :, 3] = 255
    s.mark_dirty()
    ca = ease(seg(t, 0.4, 1.4))
    text(tc, "KOTAK   ·   POSITIONS   ·   27 NOV", 300, H - BAR - 46, 40, (1, 1, 1, 0.8 * ca), spacing=6)
    fade = ease(seg(t, 0, 0.6)) * (1 - ease(seg(t, dur - 0.9, dur)))
    return s, ts, fade

SCENES = [("gold", gold_frame, 9.5), ("kotak", kotak_frame, 9.0)]
TOTAL = sum(d for _, _, d in SCENES)
def frame_at(gt, k):
    acc = 0
    for name, fn, dur in SCENES:
        if gt < acc + dur:
            s, ts, fade = fn(gt - acc, dur)
            return post(s, ts, k, fade)
        acc += dur

def main():
    if sys.argv[1] == "preview":
        os.makedirs(os.path.join(HERE, "preview_gk"), exist_ok=True)
        for x in sys.argv[2:]:
            gt = float(x); Image.fromarray(frame_at(gt, int(gt * FPS))).save(os.path.join(HERE, "preview_gk", f"t{gt:05.2f}.png")); print("still", x, flush=True)
    else:
        import imageio_ffmpeg
        out = sys.argv[2]
        w = imageio_ffmpeg.write_frames(out, (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p", quality=None,
                                        output_params=["-crf", "12", "-preset", "slow", "-profile:v", "high", "-tune", "film",
                                                       "-movflags", "+faststart", "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709"],
                                        macro_block_size=8)
        w.send(None); n = int(TOTAL * FPS)
        for k in range(n):
            w.send(frame_at(k / FPS, k).tobytes())
            if k % 24 == 0: print(f"{k}/{n}", flush=True)
        w.close(); print("WROTE", out, TOTAL, "s")

if __name__ == "__main__":
    main()
