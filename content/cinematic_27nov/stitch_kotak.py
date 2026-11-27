"""Stitch Kotak Neo positions screenshots (captured in a known scroll order) into one tall table."""
import glob, os, numpy as np
from PIL import Image
SRC = r"C:\Users\PC\.claude\projects\G--fyers-data-pipeline\f5c5a164-c198-41ad-8e93-19ad1ad01a2c\tool-results"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kotak")
X0, X1, Y0, Y1 = 410, 1560, 234, 622
# (file suffix, wheel ticks from the top) — order of capture
CHAIN = [("bjv4mb", 0), ("61bgic", 4), ("uswbu4", 6), ("yx3htl", 8), ("5l94kc", 10), ("kxm8gg", 12),
         ("ctjmyx", 14), ("nrx3pn", 16), ("fmnb7f", 18), ("mqm7zz", 23)]
load = lambda s: np.asarray(Image.open(glob.glob(SRC + rf"\*{s}.jpg")[0]).convert("RGB")).astype(np.float32)
fr = [(s, t, load(s)) for s, t in CHAIN]
def hovered_bands(a):
    b = a[Y0:Y1, 1436:1552]
    m = ((b[..., 2] > 180) & (b[..., 0] < 60)) | ((b[..., 0] > 200) & (b[..., 1] < 130) & (b[..., 2] < 80))
    return np.where(m.sum(1) > 6)[0]
def gray(a): return a[Y0:Y1, 440:1150].mean(2)
offs = [0]
for (sa, ta, a), (sb, tb, b) in zip(fr, fr[1:]):
    A, B = gray(a), gray(b)
    est = (tb - ta) * 81.5
    lo, hi = (int(est - 45), int(est + 45)) if tb - ta <= 4 else (0, Y1 - Y0 - 80)
    best = (1e9, 0)
    for d in range(max(0, lo), min(hi, Y1 - Y0 - 60)):
        e = np.abs(A[d:] - B[:(Y1 - Y0) - d]).mean()
        if e < best[0]: best = (e, d)
    offs.append(offs[-1] + best[1]); print(sb, "shift", best[1], "err", round(best[0], 2))
total = offs[-1] + (Y1 - Y0)
canvas = np.zeros((total, X1 - X0, 3), np.float32); have = np.zeros(total, int)  # 0 none, 1 hovered-source, 2 clean
for (s, t, a), o in zip(fr, offs):
    body = a[Y0:Y1, X0:X1]
    bad = np.zeros(Y1 - Y0, bool)
    for y in hovered_bands(a): bad[max(0, y - 30):y + 30] = True
    for y in range(Y1 - Y0):
        q = 1 if bad[y] else 2
        if q > have[o + y] or (q == 2 and have[o + y] == 2 and False):
            canvas[o + y] = body[y]; have[o + y] = q
print("total", total, "rows from hovered", int((have == 1).sum()), "empty", int((have == 0).sum()))
Image.fromarray(canvas.astype(np.uint8)).save(os.path.join(OUT, "body.png"))
Image.fromarray(fr[0][2][60:Y0, X0:X1].astype(np.uint8)).save(os.path.join(OUT, "header.png"))
