"""Corridor engine v1 — anchored per-frame normalization for cheap C41 lamp scans.

PHILOSOPHY (implemented, not re-invented): cheap lamp-lit C41 scans have huge
per-frame exposure spread. Roll-wide tone leaves 2-3 outlier frames flat. The
corridor pulls EVERY frame toward an anchor (full clean span, scene-correct key,
locked cast) WITHOUT breaking the series. The anchor is the REFS CLOUD (a
distribution, not a median): narrow lock bands on cast/span, wide scene-adaptive
bands on key/warmth.

MEASUREMENT SPACE: sRGB (refs are converted from their embedded ICC; candidates rendered with
output_working_space=False -> colour-managed sRGB), whatever mode the roll's recipe
renders in — the corridor needs sRGB to match the refs cloud.

Phases:
  A  calibrate  -> bands from the refs (measured by negmcp.refs, which writes
                   corridor.json + house_look.json in one pass)
  B  solve      -> per frame: render sRGB baseline, measure, solve knobs into
                   the corridor (1-2 iterations render->measure)
  C  guardrail  -> clamp locked axes (gm/span/black) to roll consensus
  D  run        -> 3 exposure-spread frames/roll, before/after contacts, tables

CLI:
  negmcp corridor calibrate     # PHASE A only -> <work_dir>/corridor/corridor.json
  negmcp corridor run <roll>    # A (if missing) + B/C/D on one roll

The roll's format and crosstalk profile come from its info.txt; the starting config is the
roll's recipe base (else the look's base_template.json), always measured in sRGB.
Paths come from the config: refs_dir, neg_root, rolls_dir, look_dir, work_dir.
Colour/look judgement is out of scope — this emits mechanics + metrics only.
"""

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image, ImageDraw

from negmcp import metrics
from negmcp.config import get_config


def render_crops(*args, **kwargs):
    """Lazy negmcp.render: calibrating / checking against refs needs no NegPy."""
    from negmcp.render import render_crops as _render_crops

    return _render_crops(*args, **kwargs)


def _refs_dir():
    return str(get_config().refs_dir)


def _out_dir():
    d = get_config().work_dir / "corridor"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _corridor_json():
    return os.path.join(_out_dir(), "corridor.json")


SEL_LONG = 900  # selection scan resolution (decode dominates; keep cheap)
SOLVE_LONG = 1200  # solve + contact resolution (medium, per docs/workflow.md)

# Solver policy: the starting base is trusted; only clearly broken frames are touched, and
# only in the safe direction (deepen a lifted black, open a thin span, de-green a real
# cast). black_point_offset is only ever moved DEEPER; yb (warm/cool) is scene colour.


def roll_base(roll):
    """(fmt, base config) for ``roll``: format + crosstalk from info.txt; base from the roll's
    recipe when it exists, else the look's base_template.json; forced to sRGB output."""
    from negmcp.exif import RollFormat, parse_info_txt, roll_format
    from negmcp.recipe import load_recipe

    cfg = get_config()
    roll_dir = cfg.roll_dir(roll)
    info = parse_info_txt(roll_dir / "info.txt")
    rf = roll_format(info)
    if rf is None:
        raise ValueError(f"{roll_dir}/info.txt has no usable `format:` line (half-frame / 35mm)")
    recipe_path = cfg.recipe_path(roll_dir.name)
    src = recipe_path if recipe_path.is_file() else cfg.look_dir / "base_template.json"
    base = dict(load_recipe(src)["base"])
    if not recipe_path.is_file() and info.get("negpy_crosstalk_profile"):
        base["crosstalk_profile"] = info["negpy_crosstalk_profile"]
    base["output_working_space"] = False
    return ("half-frame" if rf is RollFormat.HALF else "35mm"), base


# --- Seed gains (from characterize_knobs metrics.csv, working-space; sign is
#     exact, magnitude is a seed — the iterative re-measure absorbs sRGB error) -
G_SPAN_WPO = 0.5  # d(span)/d(white_point_offset)
G_KEY_BPO = 0.53  # d(key=p50)/d(black_point_offset)
G_GM_WBM = -0.60  # d(gm)/d(wb_magenta)   (wbm+ = de-green)
G_YB_WBY = 0.49  # d(yb)/d(wb_yellow)    (wby+ = warmer)
WB_DAMP = 0.65  # under-shoot WB steps: gains are seeds + wby<->gm cross-
# coupling makes a full step overshoot/oscillate on frames
# whose baseline is far out (Ektar gm=+0.5). 2 damped
# iterations converge instead of ringing.

LUMA = metrics.LUMA


# ---------------------------------------------------------------------------
# Metric vector (sRGB) — the shared negmcp.metrics fingerprint, in corridor naming
# ---------------------------------------------------------------------------
def measure(pil_img):
    return measure_fp(metrics.fingerprint(pil_img))


def measure_fp(fp):
    """A ``metrics.fingerprint`` in corridor naming (the per-ref row of corridor.json)."""
    r, g, b = fp["mid_wb"]
    return dict(
        span=round(fp["span"], 4),
        black=round(fp["black"], 4),
        white=round(fp["white"], 4),
        key=round(fp["key"], 4),
        sat=round(fp["sat"], 4),
        gm=round(fp["gm"], 4),
        yb=round(fp["yb"], 4),
        r=round(r, 4),
        g=round(g, 4),
        b=round(b, 4),
        clip=round(fp["clip"], 5),
        midfrac=round(fp["midfrac"], 4),
    )


# --- WB measured on the ADAPTIVE least-saturated (neutral) region ------------
# v3 FIX B: a cast inflates saturation, so a fixed sat<0.12 gate lets a green
# pavement HIDE (chicken-egg — v2 left it no-op). Instead take the frame's
# least-saturated region RELATIVELY: threshold = clip(30th-pct of mid-luma
# saturation, 0.10, 0.35). On a clean frame this stays low (true neutrals); on a
# casty/saturated frame it rises to include the large uniformly-tinted region.
# We also return Rn (red normalised): a WB/scanner cast comes with a RED DEFICIT
# (Rn<1) — a legitimately green/warm *scene* keeps red — so Rn is the
# scene-vs-cast discriminator (same idea as qa_gate2 det_cast green_hard_rdef).
_NEUT_LO, _NEUT_HI, _NEUT_MINFRAC = 0.15, 0.88, 0.04


def neutral_wb(pil_img):
    a = np.asarray(pil_img.convert("RGB"), dtype=np.float32) / 255.0
    lum = a @ LUMA
    mx, mn = a.max(2), a.min(2)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(mx > 0, (mx - mn) / mx, 0.0)
    mid = (lum > _NEUT_LO) & (lum < _NEUT_HI)
    if float(mid.mean()) < _NEUT_MINFRAC:
        return None
    thr = float(np.clip(np.percentile(s[mid], 30), 0.10, 0.35))
    m = mid & (s < thr)
    if float(m.mean()) < _NEUT_MINFRAC:
        return None
    px = a[m].mean(0)
    px = px / (px.mean() + 1e-9)
    r, g, b = float(px[0]), float(px[1]), float(px[2])
    gm = g - (r + b) / 2.0
    yb = (r + g) / 2.0 - b
    return dict(gm=round(gm, 4), yb=round(yb, 4), Rn=round(r, 4), frac=round(float(m.mean()), 4), thr=round(thr, 3))


# ---------------------------------------------------------------------------
# PHASE A — calibrate corridor from refs
# ---------------------------------------------------------------------------
def _dist(vals):
    v = np.asarray(vals, float)
    return dict(
        median=round(float(np.median(v)), 4),
        p10=round(float(np.percentile(v, 10)), 4),
        p90=round(float(np.percentile(v, 90)), 4),
        min=round(float(v.min()), 4),
        max=round(float(v.max()), 4),
        std=round(float(v.std()), 4),
        n=len(v),
    )


REF_EXTS = (".jpg", ".jpeg", ".png")


def refs_fingerprint(refs_dir=None):
    """(image count, sha256 over sorted name + size + mtime_ns) of the refs set.

    Metadata, not content: detects added/removed/replaced/re-exported refs without
    reading ~40 JPEGs on every start/QA run. An in-place edit always moves mtime."""
    import hashlib

    d = os.fspath(refs_dir or _refs_dir())
    h = hashlib.sha256()
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(REF_EXTS))
    for f in files:
        st = os.stat(os.path.join(d, f))
        h.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return len(files), h.hexdigest()


def load_corridor():
    """corridor.json, or None when it is missing."""
    path = _corridor_json()
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def calibrate(rows, refs_hash, measure):
    """Build + write corridor.json from per-ref rows (``measure_fp`` + ``file``).

    The refs are measured once by ``negmcp.refs`` (ICC -> sRGB), which also derives
    house_look.json from the same rows; call ``refs.ensure`` rather than this."""
    axes = {k: _dist([r[k] for r in rows]) for k in ("span", "black", "white", "key", "sat", "gm", "yb", "r", "g", "b")}

    D = axes
    corridor = dict(
        space="sRGB",
        n_refs=len(rows),
        refs_hash=refs_hash,
        measure=measure,
        calibrated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        SPAN=dict(
            kind="narrow", center=D["span"]["median"], floor=D["span"]["p10"], ceil=D["span"]["p90"], dist=D["span"]
        ),
        BLACK=dict(
            kind="lock", center=D["black"]["median"], floor=D["black"]["p10"], ceil=D["black"]["p90"], dist=D["black"]
        ),
        KEY=dict(kind="wide", center=D["key"]["median"], floor=D["key"]["p10"], ceil=D["key"]["p90"], dist=D["key"]),
        GM=dict(kind="lock", center=D["gm"]["median"], floor=D["gm"]["p10"], ceil=D["gm"]["p90"], dist=D["gm"]),
        YB=dict(kind="wide", center=D["yb"]["median"], floor=D["yb"]["p10"], ceil=D["yb"]["p90"], dist=D["yb"]),
        SAT=dict(kind="passive", center=D["sat"]["median"], dist=D["sat"]),
        WHITE=dict(dist=D["white"]),
        per_ref=rows,
    )
    corridor["_band_widths"] = {
        "span": round(D["span"]["p90"] - D["span"]["p10"], 3),
        "key": round(D["key"]["p90"] - D["key"]["p10"], 3),
        "yb": round(D["yb"]["p90"] - D["yb"]["p10"], 3),
        "gm": round(D["gm"]["p90"] - D["gm"]["p10"], 3),
        "green_channel": round(D["g"]["p90"] - D["g"]["p10"], 3),
    }
    with open(_corridor_json(), "w") as fh:
        json.dump(corridor, fh, indent=1)
    return corridor


# ---------------------------------------------------------------------------
# Frame selection — parallel scan, one candidate per crop
# ---------------------------------------------------------------------------
def _scan_worker(args):
    stem, arw, fmt, cfg = args
    try:
        crops = render_crops(arw, cfg, fmt, long_edge=SEL_LONG)
    except Exception as e:  # worker boundary: one bad ARW must not abort the scan
        return [("ERR", stem, "", repr(e))]
    out = []
    for c in crops:
        m = measure(c["image"])
        # skip blank / film-end / degenerate frames: a real photo has all three
        # midtone channels present (empty green/blank negatives collapse one
        # channel to ~0 and blow another >2, e.g. r=.05 g=2.9 b=0).
        if m["white"] < 0.06 or m["span"] < 0.03:
            continue
        if min(m["r"], m["g"], m["b"]) < 0.35 or max(m["r"], m["g"], m["b"]) > 1.95:
            continue
        out.append(("OK", stem, c["suffix"], m))
    return out


def select_frames(rolldir, fmt, base):
    arws = sorted(f for f in os.listdir(rolldir) if f.lower().endswith(".arw"))
    jobs = [(os.path.splitext(f)[0], os.path.join(rolldir, f), fmt, base) for f in arws]
    cands = []
    nw = get_config().workers
    with ProcessPoolExecutor(max_workers=nw) as ex:
        for res in ex.map(_scan_worker, jobs):
            for item in res:
                if item[0] == "ERR":
                    print(f"    [scan-err] {item[1]}: {item[3]}")
                    continue
                _, stem, suf, m = item
                cands.append(dict(stem=stem, suffix=suf, **m))
    if len(cands) < 3:
        return cands
    by_white = sorted(cands, key=lambda r: r["white"])
    thin = by_white[0]
    dense = by_white[-1]
    mid = by_white[len(by_white) // 2]
    # ensure distinct
    picks, seen = [], set()
    for tag, rec in (("thin", thin), ("mid", mid), ("dense", dense)):
        key = (rec["stem"], rec["suffix"])
        if key in seen:
            for r in by_white:
                if (r["stem"], r["suffix"]) not in seen:
                    rec = r
                    key = (r["stem"], r["suffix"])
                    break
        seen.add(key)
        picks.append((tag, rec))
    return picks, cands


# ---------------------------------------------------------------------------
# PHASE B — per-frame solve
# ---------------------------------------------------------------------------
def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# v3 balance: PUNCH flat/milky frames (deepen black + open white = stretch span
# both ways), and de-green REAL casts boldly on the adaptive neutral; still bias
# to inaction for in-band frames; never lift black, never lose span.
SPAN_MARGIN = 0.0  # act if span < floor (below refs' 10th pct = genuinely thin)
WPO_CAP = 0.12  # max extra white_point_offset (opens white; punch from top)
BLACK_TRIG = 0.08  # deepen black when measured black > this (raised/milky)
BPO_FLOOR = -0.28  # deepest black_point_offset (sRGB pins p1 ~0.063 anyway)
G_BLACK_BPO = 0.13  # d(black=p1)/d(-black_point_offset) in sRGB (probed)
KEY_WASH_M = 0.08  # darken only if key FAR above ceil (washed out)
GM_CAST_MARGIN = 0.04  # de-green if neutral gm > center+this AND red-deficit
GM_RDEF = 0.975  # neutral Rn below this = red deficit = cast (not scene)
WBM_CAP = 0.18  # max wb_magenta nudge (bolder than v2's 0.12)
# anti-flatten gate: revert ONLY if black LIFTED or span LOST vs baseline.
# Deepening black + span GAIN is the goal (punch) -> pass.
BLACK_LIFT_TOL = 0.015
SPAN_TOL = 0.02


def _render(arw, suffix, fmt, cfg):
    crops = render_crops(arw, cfg, fmt, long_edge=SOLVE_LONG)
    c = next((x for x in crops if x["suffix"] == suffix), crops[0])
    return c["image"], measure(c["image"])


def solve_frame(arw, suffix, fmt, base, corr, verbose=True):
    """v3 BALANCED staged solve. Default = do NOTHING for in-band frames. Flat/
    milky frames get PUNCH (deepen black + open white, stretching span both ways),
    never a black LIFT. Real casts (green pavement) get de-greened on the adaptive
    neutral. Tone first, then WB on the punched frame. A gate reverts only on a
    black LIFT or span LOSS vs baseline (deepening + span gain is the goal).
    Returns (baseline_m, final_m, final_cfg, touched[list], final_pil)."""
    SPAN, KEY, GM = corr["SPAN"], corr["KEY"], corr["GM"]
    base_cfg = dict(base)
    base_pil, m0 = _render(arw, suffix, fmt, base_cfg)
    cfg = dict(base_cfg)
    touched = []

    # ===== STAGE 1: TONE / PUNCH =====
    # (a) PUNCH: deepen black when it is raised/milky (never lift).
    if m0["black"] > BLACK_TRIG:
        need = m0["black"] - corr["BLACK"]["center"]  # >0, aim to centre
        d = _clamp(need / G_BLACK_BPO, 0.0, 0.30)  # bpo delta (mag)
        bpo = cfg.get("black_point_offset", 0.0)
        new_bpo = _clamp(bpo - d, BPO_FLOOR, bpo)  # only DEEPER
        if new_bpo < bpo - 0.005:
            cfg["black_point_offset"] = new_bpo
            touched.append(f"punch:bpo{new_bpo:.3f}(milky blk {m0['black']:.3f})")
    # (b) SPAN floor: open white (from the top) if thin.
    if m0["span"] < SPAN["floor"] - SPAN_MARGIN:
        need = (SPAN["floor"] + 0.01) - m0["span"]
        d = _clamp(need / G_SPAN_WPO, 0.0, WPO_CAP)
        if d > 0.005:
            cfg["white_point_offset"] = cfg.get("white_point_offset", 0.0) + d
            touched.append(f"span:wpo+{d:.3f}(thin {m0['span']:.2f})")
    # (c) washed key -> darken gently.
    if m0["key"] > KEY["ceil"] + KEY_WASH_M:
        cfg["density"] = cfg.get("density", 1.0) + 0.08
        touched.append(f"key:density+0.08(washed {m0['key']:.2f})")
    # (d) blown highlights -> hold shoulder.
    if m0["clip"] > 0.02:
        cfg["shoulder"] = _clamp(cfg.get("shoulder", 0.0) + 0.2, 0.0, 0.4)
        touched.append(f"clip:shoulder+0.2({m0['clip'] * 100:.1f}%)")

    m1, pil1 = m0, base_pil
    if touched:
        pil1, m1 = _render(arw, suffix, fmt, cfg)

    # ===== STAGE 2: de-green REAL casts on the adaptive neutral (punched frame) =
    nw = neutral_wb(pil1)
    wb_touched = []
    if nw is not None:
        gm_n, Rn = nw["gm"], nw["Rn"]
        cast_green = gm_n > GM["center"] + GM_CAST_MARGIN and Rn < GM_RDEF
        cast_mag = gm_n < GM["center"] - GM_CAST_MARGIN and Rn > (2 - GM_RDEF)
        if cast_green:  # green cast -> wbm+
            d = _clamp(WB_DAMP * (gm_n - GM["center"]) / -G_GM_WBM, 0.0, WBM_CAP)
            if d > 0.005:
                cfg["wb_magenta"] = cfg.get("wb_magenta", 0.0) + d
                wb_touched.append(f"degreen:wbm+{d:.3f}(neutral gm {gm_n:+.3f} Rn {Rn:.3f})")
        elif cast_mag:  # magenta cast -> wbm-
            d = _clamp(WB_DAMP * (gm_n - GM["center"]) / -G_GM_WBM, -WBM_CAP, 0.0)
            if d < -0.005:
                cfg["wb_magenta"] = cfg.get("wb_magenta", 0.0) + d
                wb_touched.append(f"demag:wbm{d:.3f}(neutral gm {gm_n:+.3f} Rn {Rn:.3f})")
        # yb (warm/cool) NEVER touched — that is scene colour.

    mf, pilf = m1, pil1
    if wb_touched:
        pilf, mf = _render(arw, suffix, fmt, cfg)
        touched += wb_touched

    # ===== gate: revert only on black LIFT or span LOSS (punch is allowed) =====
    reverted = False
    degenerate = (
        mf["span"] < 0.10 or mf["key"] > 0.92 or mf["clip"] > 0.5 or abs(mf["gm"]) > 0.55 or abs(mf["yb"]) > 0.65
    )
    lifted = mf["black"] > m0["black"] + BLACK_LIFT_TOL  # black raised = flat
    lost_span = mf["span"] < m0["span"] - SPAN_TOL  # span lost
    if touched and (degenerate or lifted or lost_span):
        why = (
            "degenerate"
            if degenerate
            else f"lift-black({m0['black']:.3f}->{mf['black']:.3f})"
            if lifted
            else f"lost-span({m0['span']:.3f}->{mf['span']:.3f})"
        )
        cfg, mf, pilf = base_cfg, m0, base_pil
        touched = [f"REVERTED:{why}"]
        reverted = True

    if verbose:
        tag = "NO-OP" if not touched else ("REVERT" if reverted else " ".join(touched))
        print(
            f"      {os.path.basename(arw)}{suffix}: "
            f"span {m0['span']:.3f}->{mf['span']:.3f} key {m0['key']:.3f}->{mf['key']:.3f} "
            f"blk {m0['black']:.3f}->{mf['black']:.3f} gm {m0['gm']:+.3f}->{mf['gm']:+.3f} "
            f"yb {m0['yb']:+.3f}->{mf['yb']:+.3f} | {tag}"
        )
    return m0, mf, cfg, touched, pilf


def in_corridor(m, corr):
    """Which axes are IN band. Locked axes (span floor, gm, black) + wide key/yb."""
    return dict(
        span=m["span"] >= corr["SPAN"]["floor"],
        key=corr["KEY"]["floor"] <= m["key"] <= corr["KEY"]["ceil"],
        gm=corr["GM"]["floor"] <= m["gm"] <= corr["GM"]["ceil"],
        yb=corr["YB"]["floor"] <= m["yb"] <= corr["YB"]["ceil"],
    )


# ---------------------------------------------------------------------------
# PHASE C — roll coherence guardrail (locked axes only)
# ---------------------------------------------------------------------------
def guardrail(results, corr):
    """v2: REPORT-ONLY. Just surfaces whether the roll's post-solve locked-axis
    (gm) spread exceeds the refs cloud — informational. We deliberately do NOT
    clamp toward the roll median (that is the roll_lock anti-pattern: on a
    content-skewed roll the median is a bad centre and would drag good frames).
    Coherence is instead achieved by every frame respecting the same ref-cloud
    lock band, not by a shared roll baseline."""
    if len(results) < 3:
        return results, {}
    gms = [r["after"]["gm"] for r in results]
    ref_gm_spread = corr["GM"]["ceil"] - corr["GM"]["floor"]
    roll_gm_spread = max(gms) - min(gms)
    note = dict(
        ref_gm_spread=round(ref_gm_spread, 4),
        roll_gm_spread=round(roll_gm_spread, 4),
        exceeds_ref_cloud=roll_gm_spread > ref_gm_spread,
        applied="none (report-only; no roll-median clamp)",
    )
    return results, note


# ---------------------------------------------------------------------------
# Contact sheets
# ---------------------------------------------------------------------------
def _tile(pil, w=380):
    im = pil.convert("RGB")
    return im.resize((w, max(1, int(im.height * w / im.width))), Image.LANCZOS)


def _mlabel(m):
    return (
        f"span={m['span']:.2f} key={m['key']:.2f} "
        f"gm={m['gm']:+.02f} yb={m['yb']:+.02f} sat={m['sat']:.2f} clip={m['clip'] * 100:.1f}%"
    )


def before_after_contact(roll, results, corr, out_png):
    tw, lblh, pad, hdr = 380, 34, 6, 40
    rowimgs = []
    for r in results:
        bt, at = _tile(r["before_pil"], tw), _tile(r["after_pil"], tw)
        rowimgs.append((r, bt, at))
    th = max(max(bt.height, at.height) for _, bt, at in rowimgs)
    W = 2 * (tw + pad) + pad
    H = hdr + len(rowimgs) * (th + 2 * lblh + pad) + pad
    canvas = Image.new("RGB", (W, H), (24, 24, 24))
    d = ImageDraw.Draw(canvas)
    tgt = (
        f"TARGET span>={corr['SPAN']['floor']:.2f} key[{corr['KEY']['floor']:.2f},"
        f"{corr['KEY']['ceil']:.2f}] gm[{corr['GM']['floor']:+.2f},{corr['GM']['ceil']:+.2f}] "
        f"yb[{corr['YB']['floor']:+.2f},{corr['YB']['ceil']:+.2f}]"
    )
    d.text((pad, 4), f"{roll}   BEFORE (baseline) | AFTER (corridor)", fill=(240, 240, 240))
    d.text((pad, 22), tgt, fill=(150, 180, 220))
    y = hdr
    for r, bt, at in rowimgs:
        t = r.get("touched") or []
        verdict = "NO-OP (=before)" if not t else " ; ".join(t)
        d.text(
            (pad, y), f"[{r['tag']}] {r['stem']}{r['suffix']}   BEFORE  {_mlabel(r['before'])}", fill=(210, 180, 140)
        )
        d.text((pad + tw + pad, y), f"AFTER  {_mlabel(r['after'])}", fill=(140, 210, 160))
        d.text((pad + tw + pad, y + 16), f"touched: {verdict}", fill=(200, 200, 120))
        canvas.paste(bt, (pad, y + lblh))
        canvas.paste(at, (pad + tw + pad, y + lblh))
        y += th + 2 * lblh + pad
    canvas.save(out_png)
    return out_png


# ---------------------------------------------------------------------------
# PHASE D — driver
# ---------------------------------------------------------------------------
def run_roll(roll, corr):
    fmt, base = roll_base(roll)
    rolldir = str(get_config().roll_dir(roll))
    roll = os.path.basename(rolldir)
    meta = dict(fmt=fmt, ct=base.get("crosstalk_profile"))
    print(f"\n=== {roll} ({fmt}, ct={meta['ct']}) ===")
    t0 = time.time()
    sel = select_frames(rolldir, fmt, base)
    if isinstance(sel, tuple):
        picks, cands = sel
    else:
        print(f"  [warn] only {len(sel)} candidates; skipping")
        return None
    print(
        "  selected: "
        + ", ".join(f"{tag}={r['stem']}{r['suffix']}(w={r['white']:.2f},sp={r['span']:.2f})" for tag, r in picks)
    )

    results = []
    arw_by_stem = {
        os.path.splitext(f)[0]: os.path.join(rolldir, f) for f in os.listdir(rolldir) if f.lower().endswith(".arw")
    }
    for tag, rec in picks:
        arw = arw_by_stem[rec["stem"]]
        bm, fm, cfg, touched, after_pil = solve_frame(arw, rec["suffix"], fmt, base, corr)
        # baseline pil for the contact (same base solve_frame used internally)
        bcrops = render_crops(arw, base, fmt, long_edge=SOLVE_LONG)
        bpil = next((x for x in bcrops if x["suffix"] == rec["suffix"]), bcrops[0])["image"]
        results.append(
            dict(
                tag=tag,
                stem=rec["stem"],
                suffix=rec["suffix"],
                before=bm,
                after=fm,
                cfg=cfg,
                touched=touched,
                before_pil=bpil,
                after_pil=after_pil,
            )
        )

    results, guard = guardrail(results, corr)
    contact = before_after_contact(roll, results, corr, os.path.join(_out_dir(), f"{roll}_before_after.jpg"))

    # trim pils before json
    slim = []
    for r in results:
        rr = {k: v for k, v in r.items() if not k.endswith("_pil")}
        slim.append(rr)
    summary = dict(
        roll=roll,
        meta=meta,
        picks=[t for t, _ in picks],
        frames=slim,
        guardrail=guard,
        contact=contact,
        elapsed_s=round(time.time() - t0, 1),
    )
    with open(os.path.join(_out_dir(), f"{roll}_corridor.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(f"  contact -> {contact}  ({summary['elapsed_s']}s)")
    return summary


def main(args):
    """``args``: ["calibrate"] | ["run", <roll>]."""
    from negmcp import refs

    st = refs.ensure(force=bool(args) and args[0] == "calibrate")
    corr = st.corridor
    print(f"[A] corridor.json ({st.corridor_why or 'up to date'}; band widths {corr.get('_band_widths')})")
    if args and args[0] == "calibrate":
        return
    if len(args) < 2:
        raise ValueError("corridor run needs a roll: negmcp corridor run <roll>")
    summary = run_roll(args[1], corr)
    print(f"\n[D] done -> {_out_dir()}" if summary else "\n[D] nothing to solve")
