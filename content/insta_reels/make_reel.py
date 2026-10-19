"""Instagram Reel: 'What people think trading is' vs 'What it actually is' (quant).
1080x1920, 30 fps, no audio (add trending audio in Instagram)."""
import sys, os, math
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pylib"))
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio_ffmpeg

W, H, FPS = 1080, 1920, 30
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "reel.mp4")
PREV = os.path.join(HERE, "preview"); os.makedirs(PREV, exist_ok=True)
BG = "#05070d"; GREEN = "#00ff9c"; CYAN = "#22d3ee"; RED = "#ff3b5c"; YEL = "#ffd60a"
F = "C:/Windows/Fonts/"
def font(name, size): return ImageFont.truetype(F + name, size)
BOLD = lambda s: font("arialbd.ttf", s)
MONO = lambda s: font("consola.ttf", s)
MONOB = lambda s: font("consolab.ttf", s)
rng = np.random.default_rng(7)

plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": "white", "axes.edgecolor": "#334",
                     "axes.labelcolor": "#aab", "xtick.color": "#778", "ytick.color": "#778",
                     "axes.facecolor": BG, "figure.facecolor": BG, "mathtext.fontset": "cm"})

def newfig():
    return plt.figure(figsize=(W / 100, H / 100), dpi=100)

def fig_arr(fig):
    fig.canvas.draw()
    return np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()

def ease(x): return 1 - (1 - min(max(x, 0), 1)) ** 3

def indian(n):
    s = f"{abs(int(round(n)))}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2: parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s

# ---------------------------------------------------------------- overlays
def caption(img, idx, total, label):
    d = ImageDraw.Draw(img)
    f = MONOB(44); txt = f"[{idx:02d}/{total:02d}]  {label}"
    x, y = 60, 150
    bb = d.textbbox((x, y), txt, font=f)
    d.rectangle((bb[0] - 18, bb[1] - 14, bb[2] + 18, bb[3] + 14), fill=(0, 0, 0))
    d.rectangle((bb[0] - 18, bb[1] - 14, bb[0] - 10, bb[3] + 14), fill=GREEN)
    d.text((x, y), txt, font=f, fill=GREEN)
    return img

def glitch(arr, k):
    """k = frame index inside a clip: white flash + RGB split on the first frames."""
    if k > 3: return arr
    a = arr.astype(np.int16)
    sh = [26, 14, 6, 2][k]
    a[:, :, 0] = np.roll(a[:, :, 0], sh, axis=1); a[:, :, 2] = np.roll(a[:, :, 2], -sh, axis=1)
    if k < 2:
        a = a + (255 - a) * [0.75, 0.3][k]
    # random slice displacement
    for _ in range(4 - k):
        y0 = rng.integers(0, H - 80); hgt = rng.integers(20, 80)
        a[y0:y0 + hgt] = np.roll(a[y0:y0 + hgt], rng.integers(-60, 60), axis=1)
    return np.clip(a, 0, 255).astype(np.uint8)

def vignette():
    y, x = np.mgrid[0:H, 0:W]
    r = np.sqrt(((x - W / 2) / (W / 2)) ** 2 + ((y - H / 2) / (H / 2)) ** 2)
    return np.clip(1.15 - 0.45 * r, 0.35, 1)[..., None]
VIG = vignette()

# ---------------------------------------------------------------- scene 1: what people think
def scene_think(n):
    frames = []
    pts = [(0, .55), (.15, .5), (.3, .58), (.45, .45), (.6, .4), (.75, .3), (.9, .18), (1, .1)]
    for i in range(n):
        t = i / FPS
        img = Image.new("RGB", (W, H), "#f4f4f6"); d = ImageDraw.Draw(img)
        d.text((W / 2, 230), "WHAT PEOPLE THINK", font=BOLD(84), fill="#111", anchor="mm")
        d.text((W / 2, 340), "TRADING IS...", font=BOLD(84), fill="#111", anchor="mm")
        # phone card
        cx0, cy0, cx1, cy1 = 140, 470, 940, 1650
        d.rounded_rectangle((cx0, cy0, cx1, cy1), 60, fill="white", outline="#ddd", width=4)
        d.text((cx0 + 60, cy0 + 60), "NIFTY 50", font=BOLD(46), fill="#111")
        up = t > 1.3
        price = 25000 + (t * 180 if up else t * 15)
        d.text((cx0 + 60, cy0 + 125), f"₹{price:,.0f}", font=BOLD(70), fill="#0a8f4f" if up else "#111")
        # simple chart
        gx0, gy0, gx1, gy1 = cx0 + 60, cy0 + 260, cx1 - 60, cy0 + 760
        prog = min(1, 0.35 + t / 2.2)
        poly = [(gx0 + (gx1 - gx0) * px, gy0 + (gy1 - gy0) * py) for px, py in pts if px <= prog]
        if len(poly) > 1: d.line(poly, fill="#0a8f4f", width=10, joint="curve")
        # buttons
        b1 = (cx0 + 60, cy1 - 300, cx0 + 370, cy1 - 140); b2 = (cx1 - 370, cy1 - 300, cx1 - 60, cy1 - 140)
        press1 = 1.0 < t < 1.35; press2 = 2.7 < t < 3.05
        def btn(b, col, txt, pressed):
            o = 10 if pressed else 0
            d.rounded_rectangle((b[0] + o, b[1] + o, b[2] - o, b[3] - o), 40, fill=col)
            d.text(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), txt, font=BOLD(66), fill="white", anchor="mm")
        btn(b1, "#16a34a", "BUY", press1); btn(b2, "#dc2626", "SELL", press2)
        # finger tap ripple
        for t0, b in ((1.0, b1), (2.7, b2)):
            if t0 - 0.35 < t < t0 + 0.5:
                cx, cy = (b[0] + b[2]) / 2 + 40, (b[1] + b[3]) / 2 + 30
                r = 30 + max(0, t - t0) * 160
                d.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), fill=(60, 60, 60))
                if t > t0: d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(80, 80, 80), width=6)
        if t > 3.0:
            d.text((W / 2, 1760), "+₹500  EASY MONEY", font=BOLD(80), fill="#0a8f4f", anchor="mm")
        elif t > 1.4:
            d.text((W / 2, 1760), "go up pls", font=BOLD(70), fill="#888", anchor="mm")
        frames.append(np.asarray(img))
    return frames

def scene_title(n):
    frames = []
    for i in range(n):
        img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
        d.text((W / 2, 820), "MEANWHILE,", font=BOLD(80), fill="#889", anchor="mm")
        d.text((W / 2, 960), "WHAT TRADING", font=BOLD(110), fill="white", anchor="mm")
        d.text((W / 2, 1090), "ACTUALLY IS:", font=BOLD(110), fill=GREEN, anchor="mm")
        a = np.asarray(img)
        frames.append(glitch(a, i % 6 if i < 10 else 9))
    return frames

# ---------------------------------------------------------------- formula renderer
def formula_img(lines, size=58, color="white"):
    fig = plt.figure(figsize=(10.8, 1.6 * len(lines)), dpi=100); fig.patch.set_alpha(0)
    for k, (tx, sz, col) in enumerate(lines):
        fig.text(0.5, 1 - (k + 0.5) / len(lines), tx, fontsize=sz, color=col, ha="center", va="center")
    fig.canvas.draw()
    a = np.asarray(fig.canvas.buffer_rgba()).copy(); plt.close(fig)
    return Image.fromarray(a)

def base_dark(): return Image.new("RGB", (W, H), BG)

def grid_bg():
    img = base_dark(); d = ImageDraw.Draw(img)
    for x in range(0, W, 60): d.line((x, 0, x, H), fill=(14, 20, 32))
    for y in range(0, H, 60): d.line((0, y, W, y), fill=(14, 20, 32))
    return img
GRID = grid_bg()

def scene_formula(n, lines, sub=None):
    fimg = formula_img(lines)
    frames = []
    for i in range(n):
        p = ease(i / (n * 0.55))
        img = GRID.copy()
        s = 1.0 + 0.08 * i / n
        f2 = fimg.resize((int(fimg.width * s), int(fimg.height * s)))
        # wipe reveal
        mask = Image.new("L", f2.size, 0); ImageDraw.Draw(mask).rectangle((0, 0, int(f2.width * p), f2.height), fill=255)
        alpha = Image.fromarray(np.minimum(np.asarray(f2.split()[3]), np.asarray(mask)))
        img.paste(f2, ((W - f2.width) // 2, (H - f2.height) // 2), alpha)
        if sub:
            ImageDraw.Draw(img).text((W / 2, H / 2 + f2.height / 2 + 90), sub, font=MONO(40), fill="#8899aa", anchor="mm")
        frames.append(np.asarray(img))
    return frames

# ---------------------------------------------------------------- book page
def scene_book(n, path, label, focus=(0.5, 0.45)):
    page = Image.open(path).convert("RGB")
    frames = []
    for i in range(n):
        t = i / n
        z = 1.05 + 0.55 * ease(t)
        pw = int(W * 0.95 * z); ph = int(page.height * pw / page.width)
        pg = page.resize((pw, ph), Image.BILINEAR).rotate(-3 + 2 * t, expand=True, fillcolor=BG)
        img = base_dark()
        fx = int(W / 2 - pg.width * (focus[0] - 0.5) * ease(t)); fy = int(H / 2 - pg.height * (focus[1] - 0.5) * ease(t))
        img.paste(pg, (fx - pg.width // 2, fy - pg.height // 2))
        a = (np.asarray(img) * VIG).astype(np.uint8)
        im = Image.fromarray(a); d = ImageDraw.Draw(im)
        d.rectangle((0, H - 230, W, H - 120), fill=(0, 0, 0))
        d.text((W / 2, H - 175), label, font=MONOB(40), fill=YEL, anchor="mm")
        frames.append(np.asarray(im))
    return frames

# ---------------------------------------------------------------- charts
def scene_montecarlo(n):
    steps, paths = 252, 160
    r = rng.normal(0.0004, 0.012, (paths, steps)); S = 100 * np.exp(np.cumsum(r, 1))
    fig = newfig(); ax = fig.add_axes([0.08, 0.28, 0.88, 0.44])
    cols = plt.cm.cool(np.linspace(0, 1, paths))
    lines = [ax.plot([], [], lw=1.1, color=cols[k], alpha=0.55)[0] for k in range(paths)]
    mean, = ax.plot([], [], lw=5, color=YEL)
    ax.set_xlim(0, steps); ax.set_ylim(S.min() * 0.97, S.max() * 1.02); ax.set_xticks([]); ax.set_yticks([])
    fig.text(0.5, 0.80, r"$dS_t=\mu S_t\,dt+\sigma S_t\,dW_t$", fontsize=56, ha="center")
    fig.text(0.5, 0.22, "10,000 futures. One gets realised.", fontsize=34, color="#99a", ha="center")
    frames = []
    for i in range(n):
        k = max(2, int(steps * ease(i / (n * 0.85))))
        for j, l in enumerate(lines): l.set_data(np.arange(k), S[j, :k])
        mean.set_data(np.arange(k), S[:, :k].mean(0))
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

def supertrend(df, period=10, mult=3.0):
    h, l, c = df.high.values, df.low.values, df.close.values
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    atr = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().values
    mid = (h + l) / 2; ub = mid + mult * atr; lb = mid - mult * atr
    fu, fl, st, dr = ub.copy(), lb.copy(), np.zeros(len(c)), np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        dr[i] = 1 if c[i] > fu[i - 1] else (-1 if c[i] < fl[i - 1] else dr[i - 1])
        st[i] = fl[i] if dr[i] == 1 else fu[i]
    return st, dr

def scene_supertrend(n, df):
    df = df.tail(160).reset_index(drop=True)
    full = pd.read_csv(os.path.join(HERE, "data", "nifty_h.csv"))
    st_all, dr_all = supertrend(full); st, dr = st_all[-160:], dr_all[-160:]
    fig = newfig(); ax = fig.add_axes([0.06, 0.27, 0.9, 0.46])
    ax.set_xlim(-1, 161); ax.set_ylim(df.low.min() * 0.995, df.high.max() * 1.005); ax.set_xticks([])
    fig.text(0.5, 0.82, r"$ST = \frac{H+L}{2} \pm 3 \times ATR_{10}$", fontsize=56, ha="center")
    fig.text(0.5, 0.21, "NIFTY 50 · 1H · Supertrend(10, 3) · real data", fontsize=32, color="#99a", ha="center")
    frames = []; drawn = 0
    for i in range(n):
        k = max(1, int(160 * ease(i / (n * 0.85))))
        for j in range(drawn, k):
            o, hh, ll, c = df.loc[j, ["open", "high", "low", "close"]]
            col = GREEN if c >= o else RED
            ax.vlines(j, ll, hh, color=col, lw=1.5)
            ax.add_patch(plt.Rectangle((j - 0.35, min(o, c)), 0.7, max(abs(c - o), 0.5), color=col))
            if j > 0 and dr[j] == dr[j - 1]:
                ax.plot([j - 1, j], [st[j - 1], st[j]], color=GREEN if dr[j] == 1 else RED, lw=4)
        drawn = k
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

def scene_corr(n):
    px = pd.read_csv(os.path.join(HERE, "data", "closes.csv"), index_col=0, parse_dates=True)
    cm = np.log(px).diff().dropna().corr().values; names = px.columns
    fig = newfig(); ax = fig.add_axes([0.2, 0.25, 0.75, 0.42])
    ax.set_xticks(range(20)); ax.set_yticks(range(20))
    ax.set_xticklabels(names, rotation=90, fontsize=13); ax.set_yticklabels(names, fontsize=13)
    fig.text(0.5, 0.80, r"$\rho_{ij}=\frac{\mathrm{Cov}(r_i,r_j)}{\sigma_i\,\sigma_j}$", fontsize=60, ha="center")
    fig.text(0.5, 0.17, "20 NIFTY stocks · daily returns · 2023–2026", fontsize=32, color="#99a", ha="center")
    frames = []
    for i in range(n):
        p = ease(i / (n * 0.8))
        m = np.where(np.add.outer(np.arange(20), np.arange(20)) <= p * 40, cm, np.nan)
        for im in list(ax.images): im.remove()
        ax.imshow(m, cmap="magma", vmin=0, vmax=1)
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

def scene_pairs(n):
    px = pd.read_csv(os.path.join(HERE, "data", "closes.csv"), index_col=0, parse_dates=True)[["NTPC", "POWERGRID"]].dropna()
    y, x = np.log(px.NTPC), np.log(px.POWERGRID)
    beta = np.polyfit(x, y, 1)[0]; s = y - beta * x
    z = ((s - s.rolling(60).mean()) / s.rolling(60).std()).dropna().values
    N = len(z)
    fig = newfig(); ax = fig.add_axes([0.08, 0.28, 0.88, 0.42])
    ax.set_xlim(0, N); ax.set_ylim(-3.6, 3.6); ax.set_xticks([])
    for lv, c in ((2, RED), (-2, GREEN), (0, "#556")):
        ax.axhline(lv, color=c, lw=2, ls="--" if lv else "-")
    ln, = ax.plot([], [], color=CYAN, lw=2.2)
    fig.text(0.5, 0.80, r"$z_t=\frac{s_t-\mu_s}{\sigma_s},\quad s_t=\ln P^A_t-\beta\ln P^B_t$", fontsize=46, ha="center")
    fig.text(0.5, 0.21, f"NTPC / POWERGRID  ·  β = {beta:.2f}  ·  real data", fontsize=32, color="#99a", ha="center")
    entries = [k for k in range(1, N) if (abs(z[k]) >= 2 > abs(z[k - 1]))]
    frames = []; scat = None
    for i in range(n):
        k = max(2, int(N * ease(i / (n * 0.85))))
        ln.set_data(np.arange(k), z[:k])
        if scat: scat.remove()
        e = [j for j in entries if j < k]
        scat = ax.scatter(e, z[e], s=160, color=YEL, zorder=5, edgecolor="black")
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

def scene_volsurface(n):
    from mpl_toolkits.mplot3d import Axes3D  # noqa
    m = np.linspace(0.8, 1.2, 40); T = np.linspace(0.05, 1, 40); M, TT = np.meshgrid(m, T)
    iv = 0.13 + 0.9 * (M - 1.02) ** 2 / np.sqrt(TT) + 0.04 * np.exp(-TT * 3)
    fig = newfig(); ax = fig.add_axes([-0.05, 0.22, 1.1, 0.55], projection="3d"); ax.set_facecolor(BG)
    ax.plot_surface(M, TT, iv, cmap="plasma", linewidth=0, antialiased=True)
    for a in (ax.xaxis, ax.yaxis, ax.zaxis): a.set_pane_color((0, 0, 0, 0))
    ax.set_xlabel("K / S", fontsize=18, labelpad=14); ax.set_ylabel("Expiry", fontsize=18, labelpad=14); ax.set_zlabel("IV", fontsize=18)
    fig.text(0.5, 0.82, r"$\sigma_{imp}(K,T)$", fontsize=64, ha="center")
    fig.text(0.5, 0.18, "Implied volatility surface — the smile", fontsize=32, color="#99a", ha="center")
    frames = []
    for i in range(n):
        ax.view_init(elev=24, azim=-60 + 70 * i / n)
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

KW = {"def", "class", "return", "if", "elif", "else", "for", "while", "in", "not", "and", "or", "import", "from", "self", "None", "True", "False"}
def scene_code(n, path):
    src = open(path, encoding="utf-8").read().splitlines()
    start = next((k for k, l in enumerate(src) if l.startswith("class ")), 0)
    src = src[start:start + 160]
    f = MONO(30); lh = 40
    canvas = Image.new("RGB", (W, lh * len(src) + 40), "#0b0f17"); d = ImageDraw.Draw(canvas)
    for k, line in enumerate(src):
        y = 20 + k * lh; d.text((20, y), f"{start + k + 1:4d}", font=f, fill="#3b4a5e")
        x = 120
        for tok in line.replace("\t", "    ").split(" "):
            word = tok.strip("():,.[]")
            col = CYAN if word in KW else (YEL if tok.lstrip().startswith("#") else ("#e6e6e6"))
            if "\"" in tok or "'" in tok: col = "#a3e635"
            d.text((x, y), tok + " ", font=f, fill=col); x += d.textlength(tok + " ", font=f)
    frames = []
    for i in range(n):
        off = int((canvas.height - 1300) * ease(i / n) * 0.9)
        img = base_dark(); img.paste(canvas.crop((0, off, W, off + 1300)), (0, 330))
        dd = ImageDraw.Draw(img)
        dd.text((W / 2, 1720), "backtesting/options_st_directional/engine.py", font=MONO(32), fill="#8899aa", anchor="mm")
        frames.append(np.asarray(img))
    return frames

def scene_equity(n):
    s = pd.read_csv(os.path.join(HERE, "data", "equity.csv"), index_col=0, parse_dates=True)["pnl"]
    eq = s.cumsum().values; dd = eq - np.maximum.accumulate(eq); N = len(eq)
    final, mdd = eq[-1], dd.min()
    fig = newfig()
    ax = fig.add_axes([0.1, 0.40, 0.86, 0.33]); ax2 = fig.add_axes([0.1, 0.25, 0.86, 0.12])
    ax.set_xlim(0, N); ax.set_ylim(min(eq.min(), 0) * 1.1 - 1e5, final * 1.08); ax2.set_xlim(0, N); ax2.set_ylim(mdd * 1.2, 0)
    yrs = s.index.year.values; ticks = [np.argmax(yrs == y) for y in (2024, 2025, 2026)]
    for a in (ax, ax2): a.set_xticks(ticks); a.set_xticklabels(["2024", "2025", "2026"], fontsize=20)
    ax.set_xticklabels([]); ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"₹{v/1e5:.0f}L")); ax.tick_params(labelsize=18)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1e5:.1f}L")); ax2.tick_params(labelsize=16)
    ln, = ax.plot([], [], color=GREEN, lw=3.5)
    t1 = fig.text(0.5, 0.80, "", fontsize=96, color=GREEN, ha="center", weight="bold")
    fig.text(0.5, 0.765, "my NIFTY options strategy · 3-yr backtest · 10 lots", fontsize=30, color="#aab", ha="center")
    t2 = fig.text(0.5, 0.19, "", fontsize=34, color=RED, ha="center")
    fig.text(0.5, 0.15, "net of costs + slippage · backtest, not live P&L", fontsize=26, color="#778", ha="center")
    frames = []; fills = []
    for i in range(n):
        k = max(2, int(N * ease(i / (n * 0.75))))
        ln.set_data(np.arange(k), eq[:k])
        for f_ in fills: f_.remove()
        fills = [ax.fill_between(np.arange(k), 0, eq[:k], color=GREEN, alpha=0.12),
                 ax2.fill_between(np.arange(k), dd[:k], 0, color=RED, alpha=0.6)]
        t1.set_text(f"+₹{indian(eq[k - 1])}")
        t2.set_text(f"max drawdown  -₹{indian(-dd[:k].min())}" if k > 5 else "")
        frames.append(fig_arr(fig))
    plt.close(fig); return frames

def scene_outro(n):
    frames = []
    for i in range(n):
        p = ease(i / 20)
        img = base_dark(); d = ImageDraw.Draw(img)
        d.text((W / 2, 760), "Trading isn't", font=BOLD(90), fill="white", anchor="mm")
        d.text((W / 2, 870), "buying & selling.", font=BOLD(90), fill="white", anchor="mm")
        if i > 12:
            d.text((W / 2, 1040), "It's probability,", font=BOLD(90), fill=GREEN, anchor="mm")
            d.text((W / 2, 1150), "math & code.", font=BOLD(90), fill=GREEN, anchor="mm")
        d.text((W / 2, 1700), "Education only · not investment advice", font=MONO(30), fill="#667", anchor="mm")
        a = (np.asarray(img) * p).astype(np.uint8)
        frames.append(a)
    return frames

# ---------------------------------------------------------------- assemble
def main():
    hd = os.path.join(HERE, "hd")
    sec = lambda s: int(s * FPS)
    C = sec(1.0)
    clips = [
        ("BLACK-SCHOLES", lambda: scene_formula(C, [(r"$C=S_0N(d_1)-Ke^{-rT}N(d_2)$", 52, "white"),
                                                    (r"$d_1=\frac{\ln(S_0/K)+(r+\sigma^2/2)T}{\sigma\sqrt{T}}$", 52, CYAN)], "option pricing")),
        ("MONTE CARLO", lambda: scene_montecarlo(C)),
        ("THE BOOKS", lambda: scene_book(C, f"{hd}/natenberg_366.png", "Natenberg — Option Volatility & Pricing", (0.5, 0.35))),
        ("SUPERTREND", lambda: scene_supertrend(C, pd.read_csv(f"{HERE}/data/nifty_h.csv"))),
        ("THE BOOKS", lambda: scene_book(C, f"{hd}/afml_265.png", "López de Prado — Advances in Financial ML", (0.5, 0.4))),
        ("CORRELATION", lambda: scene_corr(C)),
        ("PAIRS / STAT-ARB", lambda: scene_pairs(C)),
        ("THE BOOKS", lambda: scene_book(C, f"{hd}/vidya_46.png", "Vidyamurthy — Pairs Trading", (0.5, 0.5))),
        ("THE CODE", lambda: scene_code(C, "G:/fyers_data_pipeline/backtesting/options_st_directional/engine.py")),
        ("VOL SURFACE", lambda: scene_volsurface(C)),
        ("POSITION SIZING", lambda: scene_formula(C, [(r"$f^{*}=\frac{bp-q}{b}$", 70, "white"),
                                                      (r"$Sharpe=\sqrt{252}\,\frac{\bar{r}}{\sigma_r}$", 60, YEL)], "Kelly criterion · risk-adjusted return")),
        ("THE BOOKS", lambda: scene_book(C, f"{hd}/kaufman_523.png", "Kaufman — Trading Systems and Methods", (0.5, 0.45))),
        ("THE RESULT", lambda: scene_equity(sec(3.2))),
    ]
    w = imageio_ffmpeg.write_frames(OUT, (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p",
                                    output_params=["-crf", "18", "-preset", "medium", "-movflags", "+faststart"], macro_block_size=8)
    w.send(None)
    def emit(frames, tag):
        for k, f in enumerate(frames): w.send(np.ascontiguousarray(f).tobytes())
        Image.fromarray(frames[len(frames) * 2 // 3]).save(os.path.join(PREV, f"{tag}.png"))
        print("done", tag, len(frames), flush=True)
    emit(scene_think(sec(4.0)), "00_think")
    emit(scene_title(sec(1.2)), "01_title")
    total = len(clips)
    for idx, (label, fn) in enumerate(clips, 1):
        fr = fn()
        out = []
        for k, f in enumerate(fr):
            img = caption(Image.fromarray(f), idx, total, label)
            out.append(glitch(np.asarray(img), k))
        emit(out, f"{idx + 1:02d}_{label.replace('/', '').replace(' ', '_')}")
    emit(scene_outro(sec(2.8)), "99_outro")
    w.close(); print("WROTE", OUT)

if __name__ == "__main__":
    main()
