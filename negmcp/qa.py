"""QA gate — per-frame REGIONAL defect detector for a rendered roll (+ v1 global checks).

The v1 gate judged on global medians of a full-frame fingerprint; it missed
LOCAL/regional defects (rebate frames, washed skies, green skin, edge glows)
because a global median averages them away. v2 measures at MEDIUM resolution
(~1500px long edge — full-res is overkill, contact tiles are too small) and runs
five region-aware detectors per frame:

  1. edge-rebate   film-holder frame / cyan-glow / bright-white or black margin.
                   Flags an edge only when the outermost band is FLAT and DIFFERS
                   from the band just inside it (a jump) -> blue sky at the top is
                   continuous with the interior and is NOT flagged.
  2. skin-green    coherent skin-plausible blobs with low R/G (unhealthy green/pale
                   skin). Supporting evidence for the green-cast detector.
  3. sky           washed (bright + flat + desaturated, no gradient) or blown
                   (clipped to white). Saturated blue sky is healthy -> not flagged.
  4. green-cast    near-neutral midtones with a green excess G-(R+B)/2. This is the
                   primary signal for the roll's green-skin problem; it fires on the
                   green-shifted neutrals even when the face itself is in shadow.
                   Also detects magenta / cyan neutral casts.
  5. muddy/milky   lifted black + low white point + low contrast (veil / haze).
  6. global (v1)   clip (>2% of luma above 0.97), muddy-dark (white point < 0.60),
                   milky (black > 0.11 with span < 0.78) — whole-frame tone checks the
                   regional detectors above do not cover. Tone numbers come from
                   negmcp.metrics.fingerprint, the same fingerprint the MCP tools print.

Calibrated on a set of healthy reference scans (white~0.90 black~0.05 gexc~0.00) and two
35mm review rolls (a consumer C-41 stock and a saturated pro stock). Biased to OVER-flag;
the tribunal removes false positives (blue sky != rebate, warm sunset != cast).

  7. corridor (mandatory) every frame measured in sRGB against the refs corridor
                 (negmcp.corridor: span floor, key / gm / yb bands = refs p10..p90; sat has
                 no band there and is reported against the refs p10..p90 for information).
                 Pixels are converted to sRGB first when the manifest says they are in
                 another space (working-space passthrough renders). The corridor is
                 (re)calibrated automatically when missing or when the refs set changed.
                 Out-of-corridor is REVIEW, not HARD: by construction ~20 % of the refs
                 themselves sit outside each p10..p90 band.
  8. info        NegPy's own meters per frame (manifest), pool outliers (``negmcp pool``
                 cache) as fix candidates, half-frame auto-split failures (recipe note).
  9. approved    every frame vs the roll's APPROVED FINALS (``pos_root/<roll>`` or ``--approved``):
                 signed delta (ours - approved) per axis black/span/key/sat/gm/yb, both in sRGB
                 (finals: embedded ICC -> sRGB). The corridor is the broad house taste; the
                 finals are this roll's bar. Thresholds = app-replay reproduction noise (see
                 APPROVED_T). Over a threshold is REVIEW, never HARD. Matching is by stem only
                 (+ whole-frame finals split in half for our _L/_R); no EXIF/order guessing.

CLI: ``negmcp qa <roll|review-dir> [--json out.json] [--contact out.jpg] [--long 1500]``.
Exit 1 if any HARD flag present (pipeline gate); 2 if the corridor step cannot run.
"""

import io
import json
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageDraw

from negmcp.metrics import LUMA, fingerprint

__all__ = ["analyze", "approved_frame", "corridor_frame", "match_approved", "run"]

np.seterr(divide="ignore", invalid="ignore")

# --- thresholds (calibrated on refs + two review rolls) ---------------------
T = dict(
    # edge / rebate
    edge_outer=0.012,
    edge_inner=0.05,
    edge_flat=0.05,
    edge_color=0.35,
    edge_color_jump=0.22,
    edge_bright=0.88,
    edge_bright_jump=0.18,
    edge_dark=0.05,
    edge_dark_flat=0.012,
    edge_dark_jump=-0.20,
    # skin
    skin_std=0.05,
    skin_blob_min=0.004,
    skin_blob_max=0.20,
    skin_rg_hard=1.00,
    skin_rg_soft=1.05,
    # sky
    sky_bright=0.50,
    sky_frac=0.15,
    sky_wash_mean=0.80,
    sky_wash_std=0.055,
    sky_wash_sat=0.07,  # mild -> REVIEW
    sky_wash_mean_hard=0.85,
    sky_wash_std_hard=0.04,
    sky_wash_sat_hard=0.045,  # HARD
    sky_blown_frac=0.010,
    # cast (near-neutral)
    neut_sat=0.12,
    neut_lo=0.20,
    neut_hi=0.85,
    neut_min_frac=0.02,
    green_soft=0.015,
    green_hard=0.030,
    green_hard_rdef=0.97,  # HARD green needs R-deficit
    magenta=0.045,
    cyan_r=0.955,
    warm_b=0.940,
    # blown / muddy
    blown_frac=0.010,
    muddy_black=0.10,
    muddy_span=0.70,
    muddy_white=0.85,
    # v1 global tone checks
    v1_clip=0.02,
    v1_dark_white=0.60,
    v1_milky_black=0.11,
    v1_milky_span=0.78,
)


def load(path, long=1500):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    s = long / max(w, h)
    if s < 1:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)
    return np.asarray(im, np.float32) / 255.0


def _local_std(lum, k=7):
    m = cv2.blur(lum, (k, k))
    m2 = cv2.blur(lum * lum, (k, k))
    return np.sqrt(np.clip(m2 - m * m, 0, None))


def det_edges(a):
    H, W, _ = a.shape
    o = max(2, int(T["edge_outer"] * min(H, W)))
    i1 = max(o + 2, int(T["edge_inner"] * min(H, W)))
    flags = []
    sides = [
        ("top", a[:o], a[o:i1]),
        ("bottom", a[-o:], a[-i1:-o]),
        ("left", a[:, :o], a[:, o:i1]),
        ("right", a[:, -o:], a[:, -i1:-o]),
    ]
    for name, out, inn in sides:
        oL = float((out @ LUMA).mean())
        oS = float((out @ LUMA).std())
        oR = float((out[..., 0] - out[..., 2]).mean())
        iL = float((inn @ LUMA).mean())
        iR = float((inn[..., 0] - inn[..., 2]).mean())
        dL, dR = oL - iL, oR - iR
        flat = oS < T["edge_flat"]
        if flat and abs(oR) > T["edge_color"] and abs(dR) > T["edge_color_jump"]:
            tint = "cyan" if oR < 0 else "orange"
            flags.append(f"{name}:{tint}-rebate(R-B={oR:+.2f})")
        elif flat and oL > T["edge_bright"] and dL > T["edge_bright_jump"]:
            flags.append(f"{name}:bright-rebate(L={oL:.2f})")
        elif oL < T["edge_dark"] and oS < T["edge_dark_flat"] and dL < T["edge_dark_jump"]:
            flags.append(f"{name}:black-rebate(L={oL:.2f})")
    return flags


def det_skin(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(2)
    mn = a.min(2)
    s = np.where(mx > 0, (mx - mn) / mx, 0.0)
    lum = a @ LUMA
    std = _local_std(lum)
    m = (
        (b < g)
        & (b < r)
        & (r / (b + 1e-6) > 1.13)
        & (r / (b + 1e-6) < 2.8)
        & (g / (b + 1e-6) > 1.03)
        & (s > 0.10)
        & (s < 0.58)
        & (lum > 0.14)
        & (lum < 0.90)
        & (std < T["skin_std"])
    ).astype(np.uint8)
    if m.sum() < 120:
        return None, None
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    tot = m.size
    rg = r / (g + 1e-9)
    best = None
    for i in range(1, n):
        fr = stats[i, cv2.CC_STAT_AREA] / tot
        if fr < T["skin_blob_min"] or fr > T["skin_blob_max"]:
            continue
        val = float(rg[lab == i].mean())
        if best is None or val < best:
            best = val
    return best, float(m.mean())


def det_sky(a):
    H, W, _ = a.shape
    top = a[: int(H * 0.35)]
    lum = top @ LUMA
    mx = top.max(2)
    mn = top.min(2)
    sat = np.where(mx > 0, (mx - mn) / mx, 0.0)
    m = lum > T["sky_bright"]
    if m.mean() < T["sky_frac"]:
        return [], []
    sm, ss, ssat = float(lum[m].mean()), float(lum[m].std()), float(sat[m].mean())
    blown = float((lum > 0.985).mean())
    hard, review = [], []
    if blown > T["sky_blown_frac"]:
        hard.append(f"blown-sky({blown * 100:.1f}%)")
    if sm > T["sky_wash_mean_hard"] and ss < T["sky_wash_std_hard"] and ssat < T["sky_wash_sat_hard"]:
        hard.append(f"washed-sky(mean={sm:.2f} std={ss:.03f} sat={ssat:.02f})")
    elif sm > T["sky_wash_mean"] and ss < T["sky_wash_std"] and ssat < T["sky_wash_sat"]:
        review.append(f"bright/flat-sky(mean={sm:.2f} std={ss:.03f} sat={ssat:.02f}) washed?")
    return hard, review


def det_cast(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(2)
    mn = a.min(2)
    s = np.where(mx > 0, (mx - mn) / mx, 0.0)
    lum = a @ LUMA
    m = (s < T["neut_sat"]) & (lum > T["neut_lo"]) & (lum < T["neut_hi"])
    if m.mean() < T["neut_min_frac"]:
        return [], [], None
    rr, gg, bb = float(r[m].mean()), float(g[m].mean()), float(b[m].mean())
    mean = (rr + gg + bb) / 3
    Rn, Gn, Bn = rr / mean, gg / mean, bb / mean
    gexc = gg - (rr + bb) / 2
    hard, review = [], []
    # HARD only when green comes with a red deficit (scanner/WB cast, not a green scene)
    if gexc >= T["green_hard"] and Rn < T["green_hard_rdef"]:
        hard.append(f"green-cast(gexc={gexc:+.03f} R={Rn:.03f})")
    elif gexc >= T["green_soft"]:
        review.append(f"green-tint(gexc={gexc:+.03f}) skin/scene?")
    if (rr + bb) / 2 - gg > T["magenta"]:
        hard.append(f"magenta-cast(G={Gn:.03f})")
    if Rn < T["cyan_r"]:
        hard.append(f"cyan-cast(R={Rn:.03f})")
    if Bn < T["warm_b"]:
        review.append(f"warm/yellow-neutral(B={Bn:.03f}) scene?")
    return hard, review, gexc


def det_tone(a):
    fp = fingerprint(a)
    blk, wht, span = fp["black"], fp["white"], fp["span"]
    out = []
    if fp["blown"] > T["blown_frac"]:
        out.append(f"blown-highlights({fp['blown'] * 100:.1f}%)")
    if blk > T["muddy_black"] and span < T["muddy_span"] and wht < T["muddy_white"]:
        out.append(f"muddy/milky(blk={blk:.02f} wht={wht:.02f})")
    # v1 global checks
    if fp["clip"] > T["v1_clip"]:
        out.append(f"clip({fp['clip'] * 100:.1f}% > 0.97)")
    if wht < T["v1_dark_white"]:
        out.append(f"muddy-dark(wht={wht:.2f})")
    if blk > T["v1_milky_black"] and span < T["v1_milky_span"]:
        out.append(f"milky(blk={blk:.2f} span={span:.2f})")
    return out, blk, wht


def analyze(path, long=1500):
    a = load(path, long)
    hard, review, metrics = [], [], {}

    e = det_edges(a)
    if e:
        hard += ["rebate " + x for x in e]

    skin_rg, skin_fr = det_skin(a)
    metrics["skin_rg"] = None if skin_rg is None else round(skin_rg, 3)
    # skin R/G overlaps healthy refs (0.68-1.2), so it can only SURFACE, never HARD-condemn
    if skin_rg is not None and skin_rg < T["skin_rg_hard"]:
        review.append(f"skin-green(R/G={skin_rg:.03f})")
    elif skin_rg is not None and skin_rg < T["skin_rg_soft"]:
        review.append(f"skin-low(R/G={skin_rg:.03f})")

    sh, sr = det_sky(a)
    hard += sh
    review += sr

    ch, cr, gexc = det_cast(a)
    metrics["gexc"] = None if gexc is None else round(gexc, 3)
    hard += ch
    review += cr

    th, blk, wht = det_tone(a)
    metrics["black"] = round(blk, 3)
    metrics["white"] = round(wht, 3)
    hard += th

    return hard, review, metrics


def make_contact(items, out_path, long=1500):
    """items: list of (name, path). Grid of medium-res flagged frames."""
    if not items:
        return None
    thumbs = []
    for name, p in items:
        im = Image.open(p).convert("RGB")
        im.thumbnail((520, 520))
        thumbs.append((name, im))
    cols = min(4, len(thumbs))
    rows = (len(thumbs) + cols - 1) // cols
    cw = max(t.width for _, t in thumbs)
    chh = max(t.height for _, t in thumbs) + 18
    canvas = Image.new("RGB", (cols * cw, rows * chh), (30, 30, 30))
    d = ImageDraw.Draw(canvas)
    for i, (name, t) in enumerate(thumbs):
        x = (i % cols) * cw
        y = (i // cols) * chh
        canvas.paste(t, (x, y + 18))
        d.text((x + 3, y + 3), name, fill=(255, 230, 120))
    canvas.save(out_path, quality=88)
    return out_path


CORRIDOR_AXES = ("span", "key", "gm", "yb")
LIST_MAX = 15  # frames listed per section; the rest is in --json


def corridor_frame(rgb_srgb: Image.Image, corr: dict) -> dict:
    """Corridor verdict for one sRGB frame: measured axes, which are out, and sat (info)."""
    from negmcp import corridor

    m = corridor.measure(rgb_srgb)
    inside = corridor.in_corridor(m, corr)
    sat = corr["SAT"]["dist"]
    return {
        "measured": {k: m[k] for k in (*CORRIDOR_AXES, "sat", "black", "white")},
        "out": [k for k in CORRIDOR_AXES if not inside[k]],
        "sat_outside_refs": not (sat["p10"] <= m["sat"] <= sat["p90"]),
    }


def _band(corr: dict) -> str:
    return (
        f"span>={corr['SPAN']['floor']:.2f} key[{corr['KEY']['floor']:.2f},{corr['KEY']['ceil']:.2f}] "
        f"gm[{corr['GM']['floor']:+.3f},{corr['GM']['ceil']:+.3f}] yb[{corr['YB']['floor']:+.3f},{corr['YB']['ceil']:+.3f}]"
    )


def _why(rec: dict, corr: dict) -> str:
    m, parts = rec["measured"], []
    for k in rec["out"]:
        band = corr[k.upper()]
        if k == "span":
            parts.append(f"span {m['span']:.2f}<{band['floor']:.2f}")
        else:
            side = "<" if m[k] < band["floor"] else ">"
            parts.append(f"{k} {m[k]:+.3f}{side}{band['floor' if side == '<' else 'ceil']:+.3f}")
    return ", ".join(parts)


def _read_manifest(d: Path) -> dict:
    p = d / "_manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _recipe_notes(manifest: dict) -> dict:
    """The recipe's ``_`` notes this report reads (graded version, half-frame auto-split)."""
    path = manifest.get("recipe")
    if not path or not Path(path).is_file():
        return {}
    recipe = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in recipe.items() if k.startswith("_")}


def _srgb_loader(manifest: dict, long: int) -> tuple[str, Callable[[Path], Image.Image]]:
    """Our render -> sRGB at ``long``: pixels are re-expressed from the manifest's space
    (working-space passthrough renders are not sRGB). Raises OSError on a broken file."""
    space = manifest.get("pixel_color_space") or "sRGB"
    convert = None
    if space != "sRGB":
        from negmcp.render import to_srgb

        convert = to_srgb

    def load_srgb(p: Path) -> Image.Image:
        im = Image.open(p).convert("RGB")
        im.thumbnail((long, long))
        return convert(im, space) if convert else im

    return space, load_srgb


def _corridor_section(paths: list[Path], manifest: dict, long: int, out: dict) -> list[str]:
    from negmcp import refs

    try:
        st = refs.ensure()
    except OSError as exc:
        raise RuntimeError(f"corridor step cannot run (refs_dir: {exc})") from exc
    corr, why = st.corridor, st.corridor_why
    space, load_srgb = _srgb_loader(manifest, long)
    lines = [
        f"\n--- CORRIDOR (refs n={corr['n_refs']}{', recalibrated: ' + why if why else ''}; "
        f"pixels {space}{' -> sRGB' if space != 'sRGB' else ''}) ---",
        f"  bands: {_band(corr)}  (sat: info vs refs p10..p90)",
    ]
    recs = {}
    for p in paths:
        try:
            im = load_srgb(p)
        except OSError:
            continue
        recs[p.stem] = corridor_frame(im, corr)
        out.setdefault(p.stem, {})["corridor"] = recs[p.stem]
    n = len(recs)
    inside = [k for k, r in recs.items() if not r["out"]]
    per_axis = {a: sum(a in r["out"] for r in recs.values()) for a in CORRIDOR_AXES}
    n_sat = sum(r["sat_outside_refs"] for r in recs.values())
    lines.append(
        f"  inside on all axes: {len(inside)}/{n} | out: "
        + " ".join(f"{a} {c}" for a, c in per_axis.items())
        + f" | sat outside refs (info): {n_sat}"
    )
    for k in [k for k, r in recs.items() if r["out"]][:LIST_MAX]:
        lines.append(f"  {k}: {_why(recs[k], corr)}")
    if n - len(inside) > LIST_MAX:
        lines.append(f"  ... {n - len(inside) - LIST_MAX} more in --json")
    rendered_on = manifest.get("negpy_version")
    graded = manifest.get("graded_negpy_version")
    if rendered_on and graded and graded != rendered_on:
        lines.append(
            f"  ENGINE: roll graded on NegPy {graded}, now {rendered_on}: {n - len(inside)}/{n} frames outside the corridor"
        )
    elif rendered_on and not graded:
        lines.append(
            f"  ENGINE: recipe not stamped (_graded_negpy_version); rendered on {rendered_on}: {n - len(inside)}/{n} outside"
        )
    out["_corridor"] = {"bands": _band(corr), "inside": len(inside), "frames": n, "out_per_axis": per_axis}
    return lines


# --- approved finals ----------------------------------------------------------
APPROVED_AXES = ("black", "span", "key", "sat", "gm", "yb")
# Per-frame |delta| thresholds = p90 of |ours - approved| over `negmcp replay` renders of
# app-graded rolls (each frame's edits.db config re-rendered on NegPy 0.62 vs the app's own
# export of it; five C-41 rolls, 174 frames, long edge 1500). That is reproduction noise: the same edit re-rendered differs
# from its approved export this much 9 times out of 10 (p50 is ~40 % of these).
APPROVED_T = {"black": 0.046, "span": 0.113, "key": 0.085, "sat": 0.083, "gm": 0.040, "yb": 0.163}
# Roll-level drift: |median delta| above the largest |median delta| of any of those 5 rolls.
APPROVED_ROLL_T = {"black": 0.024, "span": 0.073, "key": 0.046, "sat": 0.058, "gm": 0.019, "yb": 0.070}
APPROVED_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
# whole-frame final of a half-frame pair -> our _L / _R
HALF_SPLIT_X = {"L": (0.03, 0.47), "R": (0.53, 0.97)}
HALF_SPLIT_Y = (0.05, 0.95)
_SRGB = ImageCms.createProfile("sRGB")


def match_approved(stems: list[str], approved_dir: Path) -> tuple[dict[str, tuple[Path, str | None]], list[str]]:
    """Our frame stems -> (approved file, half side or None); also the approved stems left over.

    Rules, in order: same stem; ``X_L``/``X_R`` vs a LANDSCAPE whole frame ``X`` (split in half);
    a unique ``_``-suffix match (``roll_Frame001`` vs ``Frame001``). Nothing else — export names
    the app invents (``<roll>_FrameNNN`` vs ``DSC*``) do not match, and EXIF dates are not used:
    finals are re-tagged with the shooting date, renders carry the scan time."""
    files = {
        p.stem: p
        for p in sorted(approved_dir.iterdir())
        if p.suffix.lower() in APPROVED_EXT and not p.name.startswith(("_", "."))
    }
    matched: dict[str, tuple[Path, str | None]] = {}
    by_suffix: dict[str, list[str]] = {}
    for s in stems:
        if s in files:
            matched[s] = (files[s], None)
            continue
        base, _, side = s.rpartition("_")
        if base in files and side in HALF_SPLIT_X:
            with Image.open(files[base]) as im:
                landscape = im.width > im.height
            if landscape:
                matched[s] = (files[base], side)
            continue
        cands = [k for k in files if k.endswith("_" + s) or s.endswith("_" + k)]
        if len(cands) == 1:
            by_suffix.setdefault(cands[0], []).append(s)
    for k, ours in by_suffix.items():
        if len(ours) == 1:  # an approved file claimed by two of our frames is ambiguous -> neither
            matched[ours[0]] = (files[k], None)
    used = {p for p, _ in matched.values()}
    return matched, sorted(k for k, p in files.items() if p not in used)


def _load_approved(path: Path, side: str | None, long: int) -> Image.Image:
    """An approved final as the viewer shows it: embedded ICC -> sRGB (untagged = sRGB)."""
    im = Image.open(path)
    icc = im.info.get("icc_profile")
    im = im.convert("RGB")
    if icc:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if "sRGB" not in ImageCms.getProfileDescription(src):
            im = ImageCms.profileToProfile(
                im,
                src,
                _SRGB,
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                outputMode="RGB",
                flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
            )
    if side:
        (x0, x1), (y0, y1) = HALF_SPLIT_X[side], HALF_SPLIT_Y
        w, h = im.size
        im = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    im.thumbnail((long, long))
    return im


def approved_frame(ours_srgb: Image.Image, approved_srgb: Image.Image) -> dict:
    """Signed per-axis delta ours - approved (sRGB fingerprints) and the axes over APPROVED_T."""
    a, b = fingerprint(ours_srgb), fingerprint(approved_srgb)
    delta = {k: round(float(a[k]) - float(b[k]), 4) for k in APPROVED_AXES}
    return {"delta": delta, "over": [k for k in APPROVED_AXES if abs(delta[k]) > APPROVED_T[k]]}


def _fmt_delta(delta: dict, axes) -> str:
    return " ".join(f"{k} {delta[k]:+.3f}" for k in axes)


def _approved_section(paths: list[Path], manifest: dict, long: int, approved_dir: Path | None, out: dict) -> list[str]:
    if approved_dir is None:
        out["approved"] = None
        return ["\n--- APPROVED: off (--no-approved) ---"]
    if not approved_dir.is_dir():
        out["approved"] = {"dir": str(approved_dir), "frames": 0}
        return [f"\n--- APPROVED: no approved finals ({approved_dir} missing) ---"]
    stems = [p.stem for p in paths]
    matched, unused = match_approved(stems, approved_dir)
    if not matched:
        out["approved"] = {"dir": str(approved_dir), "frames": 0, "unused": unused}
        why = f"none of {len(unused)} files match our {len(stems)} frame names" if unused else "folder empty"
        return [f"\n--- APPROVED: no approved finals matched ({approved_dir}: {why}) ---"]
    space, load_srgb = _srgb_loader(manifest, long)
    recs = {}
    for p in paths:
        if p.stem not in matched:
            continue
        ref, side = matched[p.stem]
        try:
            rec = approved_frame(load_srgb(p), _load_approved(ref, side, long))
        except OSError:
            continue
        rec["approved"] = ref.name + (f"[{side}]" if side else "")
        recs[p.stem] = rec
        out.setdefault(p.stem, {})["approved"] = rec
    n, n_split = len(recs), sum(matched[s][1] is not None for s in recs)
    med = {k: float(np.median([r["delta"][k] for r in recs.values()])) for k in APPROVED_AXES}
    drift = [k for k in APPROVED_AXES if abs(med[k]) > APPROVED_ROLL_T[k]]
    over = {k: sum(k in r["over"] for r in recs.values()) for k in APPROVED_AXES}
    over_frames = [s for s, r in recs.items() if r["over"]]
    unmatched = [s for s in stems if s not in matched]
    lines = [
        f"\n--- APPROVED (finals {approved_dir}; matched {n}/{len(stems)}"
        f"{f', {n_split} as half of a whole frame' if n_split else ''}; pixels {space}"
        f"{' -> sRGB' if space != 'sRGB' else ''}, finals ICC -> sRGB; delta = ours - approved) ---",
        "  thresholds |delta|/frame (app-replay p90): " + _fmt_delta(APPROVED_T, APPROVED_AXES).replace("+", ""),
        "  median delta: " + _fmt_delta(med, APPROVED_AXES),
        f"  REVIEW roll drift (|median| > {', '.join(f'{k} {APPROVED_ROLL_T[k]:.3f}' for k in drift)}): {', '.join(drift)}"
        if drift
        else "  roll median within app-replay noise on all axes",
        f"  frames over threshold (REVIEW): {len(over_frames)}/{n} | " + " ".join(f"{k} {c}" for k, c in over.items()),
    ]

    def worst(s: str) -> float:
        return max(abs(recs[s]["delta"][k]) / APPROVED_T[k] for k in APPROVED_AXES)

    for s in sorted(over_frames, key=worst, reverse=True)[:LIST_MAX]:
        lines.append(f"  {s}: x{worst(s):.1f} {_fmt_delta(recs[s]['delta'], recs[s]['over'])}")
    if len(over_frames) > LIST_MAX:
        lines.append(f"  ... {len(over_frames) - LIST_MAX} more in --json")
    if unmatched:
        lines.append(
            f"  WARNING: {len(unmatched)} of our frames have no approved final (not compared): "
            + ", ".join(unmatched[:8])
            + (" ..." if len(unmatched) > 8 else "")
        )
    if unused:
        lines.append(
            f"  approved finals without a render here: {len(unused)} ({', '.join(unused[:8])}{' ...' if len(unused) > 8 else ''})"
        )
    out["approved"] = {
        "dir": str(approved_dir),
        "frames": n,
        "split_half": n_split,
        "median_delta": {k: round(v, 4) for k, v in med.items()},
        "roll_drift": drift,
        "over_frames": len(over_frames),
        "over_per_axis": over,
        "thresholds": APPROVED_T,
        "roll_thresholds": APPROVED_ROLL_T,
        "unmatched": unmatched,
        "unused": unused,
    }
    return lines


def _info_section(cfg_work: Path | None, manifest: dict, notes: dict, roll: str, out: dict) -> list[str]:
    lines = []
    meters = manifest.get("negpy_metrics") or {}
    if meters:
        lines.append("\n--- NegPy meters (info; normalised negative luma, textural_range in log10 D) ---")
        keys = sorted({k for v in meters.values() for k in v})
        for k in keys:
            v = np.array([m[k] for m in meters.values() if k in m])
            lines.append(
                f"  {k:<16} median {np.median(v):.3f}  p10..p90 {np.percentile(v, 10):.3f}..{np.percentile(v, 90):.3f}"
            )
        out["_negpy_metrics"] = meters
    if cfg_work is not None:
        pool_path = cfg_work / "pool" / f"{roll}.json"
        if pool_path.is_file():
            pool = json.loads(pool_path.read_text())
            stale = (
                ""
                if pool.get("negpy_version") == manifest.get("negpy_version")
                else " (pooled on another NegPy: re-run)"
            )
            lines.append(f"\n--- POOL outliers -> per-frame fix candidates{stale} ---\n  {pool.get('outliers')}")
            out["_pool_outliers"] = pool.get("outliers")
    detect = notes.get("_half_frame_detect")
    if isinstance(detect, dict) and detect.get("failed"):
        lines.append(f"\n--- HALF-FRAME auto-split failed (DEFAULT_CROP used) ---\n  {detect['failed']}")
        out["_half_frame_detect_failed"] = detect["failed"]
    return lines


def run(
    d: Path,
    long: int = 1500,
    outj: Path | None = None,
    contact: Path | None = None,
    approved: Path | None = None,
    use_approved: bool = True,
) -> int:
    """Gate every ``d/*.jpg``; print the report; return 1 if any HARD flag, else 0.

    ``approved``: the roll's approved finals (default ``pos_root/<d.name>``); ``use_approved``
    False skips that comparison. It only reports (REVIEW) and never changes the exit code."""
    from negmcp.config import get_config

    paths = sorted(p for p in d.glob("*.jpg") if not p.name.startswith("_"))
    out = {}
    hard_frames, review_frames = {}, {}
    for p in paths:
        name = p.stem
        try:
            h, r, m = analyze(p, long)
        except (OSError, ValueError, SyntaxError) as ex:  # a broken/truncated JPG must not stop the gate
            hard_frames[name] = [f"unreadable({ex})"]
            out[name] = {"hard": hard_frames[name], "review": [], "metrics": {}}
            continue
        out[name] = {"hard": h, "review": r, "metrics": m}
        if h:
            hard_frames[name] = h
        if r:
            review_frames[name] = r

    # green-skin severity ranking: skin R/G below healthy (~1.12) + neutral green excess.
    green = []
    for n, rec in out.items():
        m = rec.get("metrics", {})
        rg = m.get("skin_rg")
        gx = m.get("gexc")
        score = 0.0
        if rg is not None:
            score += max(0.0, 1.12 - rg)
        if gx is not None:
            score += 6.0 * max(0.0, gx - 0.005)
        if score > 0.02:
            green.append((score, n, rg, gx))
    green.sort(reverse=True)

    print(f"QA gate: {len(paths)} frames | HARD {len(hard_frames)} | REVIEW {len(review_frames)}")
    if hard_frames:
        print("\n--- HARD (defect suspects) ---")
        for n in sorted(hard_frames):
            print(f"  {n}: {'; '.join(hard_frames[n])}")
    if review_frames:
        print("\n--- REVIEW (soft / scene-vs-defect) ---")
        for n in sorted(review_frames):
            print(f"  {n}: {'; '.join(review_frames[n])}")

    if green:
        print("\n--- GREEN-SKIN SUSPECTS (ranked; verify skin regions) ---")
        for score, n, rg, gx in green[:12]:
            print(f"  {n}: score={score:.03f} skinR/G={rg} gexc={gx}")

    manifest = _read_manifest(d)
    try:
        print("\n".join(_corridor_section(paths, manifest, long, out)))
    except RuntimeError as exc:
        print(f"\nCORRIDOR STEP FAILED: {exc}")
        return 2
    approved_dir = None
    if use_approved:
        approved_dir = approved or get_config().approved_dir(d.name)
        if approved_dir.resolve() == d.resolve():  # qa run on the finals themselves
            approved_dir = None
    print("\n".join(_approved_section(paths, manifest, long, approved_dir, out)))
    print("\n".join(_info_section(get_config().work_dir, manifest, _recipe_notes(manifest), d.name, out)))

    if outj:
        out["_green_ranking"] = [
            {"frame": n, "score": round(s, 3), "skin_rg": rg, "gexc": gx} for s, n, rg, gx in green
        ]
        outj.write_text(json.dumps(out, indent=1))
        print(f"\njson -> {outj}")
    if contact:
        items = [(n, d / f"{n}.jpg") for n in sorted(hard_frames)]
        made = make_contact(items, contact, long)
        if made:
            print(f"contact -> {made}")

    return 1 if hard_frames else 0
