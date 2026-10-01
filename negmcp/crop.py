"""Rebate/frame detection (geometry only, no colour/render changes).

Input: a full-scan render (PIL RGB, from negmcp.render.render(arw, cfg) with no
manual_crop_rect set) + the roll's `format` (from info.txt: "half-frame" | "35mm").
Output: a list of (rect_fraction [x0,y0,x1,y1], suffix) — one entry for 35mm,
two ("_L","_R") for half-frame.

Detection, not hardcoding: every scan has a bright, low-detail rebate/holder
border around the exposed frame(s); half-frame has a further, near-black
divider between the two halves. Both show up as a sharp step in the per-row /
per-column median-luma profile, which we locate with Otsu thresholding on the
1-D profile (bimodal: "rebate" cluster vs "frame content" cluster) plus a
run-length check so noise near the true edge can't flip the call.

DEFAULT_CROP (half-frame) / DEFAULT_CROP_35 (35mm) are the ONLY fallbacks, and
only when detection is not confident — every fallback is logged (see `warn`),
never silent.
"""

import logging

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Priors / fallback only — hand-measured half-frame crops of a half-frame camera's scans on a
# copy stand (the half-frame batch default; adjust per frame with `negmcp fix` when the gate drifts).
# Detection is expected to land close to these; when it doesn't, we fall back here
# and log loudly rather than guess.
DEFAULT_CROP = {"L": [0.075, 0.05, 0.45, 0.87], "R": [0.5375, 0.05, 0.8925, 0.87]}

# 35mm fallback: started from the *measured* rebate margins on 35mm full-scan renders
# (~3-6% each side). Re-measured directly on the 9 frames of one roll that actually land on
# this fallback (very soft, gradual rebate->content ramp under that roll's lamp --
# detect_frame_bbox can't get a confident split on them): walking each edge's luma/warmth profile until it clears the rebate (and
# discarding the handful of frames where a bright/warm piece of CONTENT, not
# rebate, drove the number up -- e.g. a sunlit wall) gives top~0.044-0.045,
# bottom~0.095-0.106, left~0.041-0.075, right~0.064-0.096 as the real worst case
# across those 9. Set with headroom above that worst case on every edge; still a
# conservative inset, used ONLY as a fallback when detection is not confident,
# and every use is logged.
DEFAULT_CROP_35 = [0.085, 0.055, 0.895, 0.885]

# --- tunables -----------------------------------------------------------------
_MIN_RUN_FRAC = 0.01  # run must persist >=1% of the dimension to count as real, not noise
_MIN_FRAME_FRAC = 0.55  # frame content must occupy >=55% of the dimension, else "not confident"
_EDGE_INSET_FRAC = 0.016  # safety inset applied inward after detection (kills residual rebate bleed);
#                            row/col medians average away a *localized* residual (e.g. one corner
#                            leaking a few px because the frame isn't perfectly parallel to the scan
#                            area) -- measured on kodak200_1 at up to ~0.002 (a handful of px) in the
#                            worst corner; doubled from 0.008 for headroom without touching real content.
_MAX_MARGIN_FRAC = 0.20  # a real rebate margin is never this wide (measured <=~0.145 worst case,
#                           kodak200_1); a "detected" margin past this is high-contrast CONTENT fooling
#                           the row/col median (e.g. a dark car against a bright sidewalk, or a dark
#                           foreground column against a bright building) that got mistaken for rebate,
#                           not real film base -- reject it (None -> fallback) rather than crop into it.
_GAP_SEARCH_LO, _GAP_SEARCH_HI = 0.30, 0.72  # relative band (of frame width) to search for the L/R divider
_GAP_MARGIN_FRAC = 0.012  # extra margin subtracted from each half at the divider

# C41 rebate is the orange film base: never cooler than neutral (R-B >= 0 in every
# measured render -- fully-clipped rebate reads a flat (0 warmth, near-max luma)
# "paper white" with the colour clipped away, and the rebate/frame gradient band
# right inside it reads warm, R-B +30..+170). Luma alone misses this when frame
# content near the border is ALSO bright (e.g. sky in an upward-looking shot) --
# rebate and sky can share luma but never share warmth: sky/water/shade trend
# measurably cooler than neutral (R-B negative, -10..-80 in this roll's frames).
# So a border pixel can only be frame content-not-rebate through the warmth route
# if it's *measurably cool*; "colourless" (R-B ~ 0, incl. clipped white) must stay
# classified by luma alone, or every rebate frame's own clipped edge gets rescued
# as "content" -- that regression is exactly why this is a guard, not an Otsu split.
_WARM_COOL_GUARD = -8.0  # R-B (0..255 scale); below this = clearly cooler than neutral

# --- aspect-ratio guard ---------------------------------------------------
# A crop whose aspect ratio strays far from the physical frame's is a crop that
# went wrong (missed divider -> half of the OTHER half included, bbox latched
# onto high-contrast content instead of the rebate edge, ...) even when the
# per-edge confidence checks above didn't reject it outright. Expected ratio is
# DERIVED from DEFAULT_CROP / DEFAULT_CROP_35 at the scan's own pixel dimensions
# (never hardcoded -- those rects are themselves measured priors, see their
# docstrings above), so this stays film/scan-agnostic.
_ASPECT_TOL_FRAC = 0.12  # relative tolerance; normal detection jitter (divider
#                           width, edge inset) is far under this, a wrong crop is not.


def _rect_pixel_ratio(rect, w: int, h: int) -> float:
    x0, y0, x1, y1 = rect
    dw, dh = (x1 - x0) * w, (y1 - y0) * h
    return dw / dh if dh > 0 else 0.0


def expected_aspect_ratio(fmt: str, w: int, h: int, suffix: str = "") -> float:
    """Expected crop width/height, derived from DEFAULT_CROP(_35) at this
    scan's actual pixel dimensions (fractions alone aren't resolution-free
    across non-square scans)."""
    fmt = (fmt or "").strip().lower()
    rect = DEFAULT_CROP["R" if suffix == "_R" else "L"] if fmt == "half-frame" else DEFAULT_CROP_35
    return _rect_pixel_ratio(rect, w, h)


def aspect_flag(rect, fmt: str, w: int, h: int, suffix: str = "") -> tuple:
    """(is_review, actual_ratio, expected_ratio). is_review=True when the
    detected crop's aspect ratio deviates from the DEFAULT_CROP-derived
    expectation by more than `_ASPECT_TOL_FRAC` (relative)."""
    actual = _rect_pixel_ratio(rect, w, h)
    expected = expected_aspect_ratio(fmt, w, h, suffix)
    if expected <= 0:
        return False, actual, expected
    rel = abs(actual / expected - 1.0)
    return rel > _ASPECT_TOL_FRAC, actual, expected


def _smooth(profile: np.ndarray, win: int) -> np.ndarray:
    win = max(1, win | 1)  # odd
    if win <= 1:
        return profile
    kernel = np.ones(win, dtype=np.float32) / win
    return np.convolve(profile, kernel, mode="same")


def _otsu(profile: np.ndarray) -> float:
    """Otsu threshold on a 1-D profile, treated as a mini bimodal histogram."""
    lo, hi = float(profile.min()), float(profile.max())
    if hi - lo < 1e-6:
        return lo
    scaled = np.clip((profile - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    t, _ = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return lo + (float(t) / 255.0) * (hi - lo)


def _longest_edge_runs(dark_mask: np.ndarray, min_run: int):
    """First run of True (from the start) and last run of True (to the end),
    each required to persist >=min_run samples. Returns (start, end) indices of
    the frame-content span, or None if no such runs exist."""
    n = len(dark_mask)
    # first run from the left
    start = None
    i = 0
    while i < n:
        if dark_mask[i]:
            j = i
            while j < n and dark_mask[j]:
                j += 1
            if j - i >= min_run:
                start = i
                break
            i = j
        else:
            i += 1
    if start is None:
        return None
    # last run from the right
    end = None
    i = n - 1
    while i >= 0:
        if dark_mask[i]:
            j = i
            while j >= 0 and dark_mask[j]:
                j -= 1
            if i - j >= min_run:
                end = i
                break
            i = j
        else:
            i -= 1
    if end is None or end <= start:
        return None
    return start, end


def _frame_extent_1d(luma_profile: np.ndarray, warm_profile: np.ndarray):
    """Locate the [start, end] span of frame-content within a bright rebate
    profile. Returns (start, end) pixel indices, or None if unconfident.

    Rebate = bright (luma), full stop -- that's still the primary signal and
    it is what every other roll is detected on. Warmth only RESCUES a bright
    border pixel back into "frame content" when it's measurably cooler than
    neutral (see _WARM_COOL_GUARD): that's the one case luma alone gets wrong
    (bright sky/water right at the border, sharing luma with the rebate but
    never its colour). It never does the opposite -- a colourless/clipped
    bright pixel (warmth ~0, e.g. fully-clipped rebate) stays classified by
    luma alone, so this can't rescue the rebate itself back into "content".
    """
    n = len(luma_profile)
    win = max(3, int(n * 0.004))
    luma_s = _smooth(luma_profile, win)
    warm_s = _smooth(warm_profile, win)
    luma_thr = _otsu(luma_s)
    bright = luma_s >= luma_thr  # rebate candidate, per luma alone
    clearly_cool = warm_s < _WARM_COOL_GUARD  # measurably cooler than neutral -> can't be rebate
    not_dark = bright & ~clearly_cool
    dark = ~not_dark  # frame content is darker than the rebate, or unambiguously cooler
    #                   than it, per the measured renders (rebate ~140-235 luma / R-B >= 0,
    #                   frame content ~15-110 luma in every test roll: 2 half-frame and
    #                   3 full-frame rolls, consumer and pro C-41 stocks).
    min_run = max(1, int(n * _MIN_RUN_FRAC))
    span = _longest_edge_runs(dark, min_run)
    if span is None:
        return None
    start, end = span
    if (end - start) < n * _MIN_FRAME_FRAC:
        return None
    if start > n * _MAX_MARGIN_FRAC or (n - end) > n * _MAX_MARGIN_FRAC:
        return None
    return start, end


def _luma(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.float32)


def _warmth(rgb: np.ndarray) -> np.ndarray:
    """R-B per pixel: the orange C41 rebate is reliably warm; ordinary frame
    content at a bright border (sky, snow, ...) trends neutral or cool."""
    return rgb[:, :, 0] - rgb[:, :, 2]


def detect_frame_bbox(image: Image.Image):
    """(top, bottom, left, right) pixel bbox of the exposed frame(s) (both
    halves, for half-frame), excluding the rebate border. None if unconfident."""
    gray = _luma(image)
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    warm = _warmth(rgb)
    h, w = gray.shape
    row_span = _frame_extent_1d(np.median(gray, axis=1), np.median(warm, axis=1))
    if row_span is None:
        return None
    top, bottom = row_span
    # Restrict the column profile to the detected frame rows so rebate rows
    # above/below can't pollute the left/right call.
    col_span = _frame_extent_1d(np.median(gray[top : bottom + 1], axis=0), np.median(warm[top : bottom + 1], axis=0))
    if col_span is None:
        return None
    left, right = col_span
    inset_h = max(1, int((bottom - top) * _EDGE_INSET_FRAC))
    inset_w = max(1, int((right - left) * _EDGE_INSET_FRAC))
    top, bottom = top + inset_h, bottom - inset_h
    left, right = left + inset_w, right - inset_w
    if bottom <= top or right <= left:
        return None
    return top, bottom, left, right


def detect_divider(gray: np.ndarray, bbox):
    """(gap_left, gap_right) absolute pixel columns of the near-black L/R
    divider inside `bbox`. None if unconfident."""
    top, bottom, left, right = bbox
    width = right - left
    col_med = np.median(gray[top : bottom + 1, left : right + 1], axis=0)
    col_med = _smooth(col_med, max(3, int(width * 0.004)))

    lo_i, hi_i = int(width * _GAP_SEARCH_LO), int(width * _GAP_SEARCH_HI)
    if hi_i - lo_i < 3:
        return None
    band = col_med[lo_i:hi_i]
    center_rel = lo_i + int(np.argmin(band))
    gap_floor = float(col_med[center_rel])

    flank_lo = col_med[int(width * 0.08) : int(width * 0.30)]
    flank_hi = col_med[int(width * 0.70) : int(width * 0.92)]
    if len(flank_lo) == 0 or len(flank_hi) == 0:
        return None
    # 80th percentile, not median: the flank bands can themselves contain dark
    # foreground/foliage right next to the divider (e.g. tree silhouettes), which
    # would drag a median-based content_level down toward the gap floor and make
    # the divider look shallower than it is -> premature stop, black-strip bleed
    # into the crop. The percentile is more robust to that contamination.
    content_level = float(np.percentile(np.concatenate([flank_lo, flank_hi]), 75))
    if content_level - gap_floor < 5.0:  # divider must be a real step darker than content
        return None
    thr = gap_floor + (content_level - gap_floor) * 0.52

    gl = center_rel
    while gl > 0 and col_med[gl - 1] < thr:
        gl -= 1
    gr = center_rel
    while gr < width - 1 and col_med[gr + 1] < thr:
        gr += 1

    gap_w = gr - gl
    if not (width * 0.01 <= gap_w <= width * 0.20):
        return None
    if not (width * 0.25 <= center_rel <= width * 0.75):
        return None
    margin = max(1, int(width * _GAP_MARGIN_FRAC))
    return left + gl - margin, left + gr + margin


def _finalize(rects_suffixes, fmt: str, w: int, h: int) -> list:
    """Attach the aspect-ratio review flag to each (rect, suffix), logging any
    flagged crop loudly (never pass a bad crop through silently)."""
    out = []
    for rect, suffix in rects_suffixes:
        flagged, actual, expected = aspect_flag(rect, fmt, w, h, suffix)
        if flagged:
            log.warning(
                "aspect-ratio guard: %s%s crop ratio=%.3f deviates >%.0f%% from expected=%.3f "
                "(rect=%s) -> flagged for review",
                fmt,
                suffix,
                actual,
                _ASPECT_TOL_FRAC * 100,
                expected,
                [round(v, 4) for v in rect],
            )
        out.append((rect, suffix, flagged))
    return out


def detect_crops(image: Image.Image, fmt: str):
    """Main entry point.

    Returns list of (rect_fraction [x0,y0,x1,y1], suffix, aspect_flag) — suffix
    is "_L"/"_R" for half-frame, "" for 35mm; aspect_flag is True when the
    crop's aspect ratio deviates from the DEFAULT_CROP-derived expectation
    (review, not a silent pass-through). Falls back to DEFAULT_CROP /
    DEFAULT_CROP_35 on any low-confidence detection, always via `log.warning`.
    """
    fmt = (fmt or "").strip().lower()
    w, h = image.size
    gray = _luma(image)  # detect_divider (half-frame only) still uses plain luma:
    #                       the near-black L/R divider is genuinely dark, not a warmth call.

    bbox = detect_frame_bbox(image)

    if fmt == "half-frame":
        if bbox is None:
            log.warning("half-frame: frame bbox detection failed -> falling back to DEFAULT_CROP for L and R")
            return _finalize([(DEFAULT_CROP["L"], "_L"), (DEFAULT_CROP["R"], "_R")], fmt, w, h)
        top, bottom, left, right = bbox
        gap = detect_divider(gray, bbox)
        if gap is None:
            log.warning(
                "half-frame: outer bbox ok (top=%.3f bot=%.3f left=%.3f right=%.3f) but L/R divider "
                "detection failed -> falling back to DEFAULT_CROP for L and R",
                top / h,
                bottom / h,
                left / w,
                right / w,
            )
            return _finalize([(DEFAULT_CROP["L"], "_L"), (DEFAULT_CROP["R"], "_R")], fmt, w, h)
        gap_left, gap_right = gap
        y0, y1 = top / h, bottom / h
        rect_l = [left / w, y0, gap_left / w, y1]
        rect_r = [gap_right / w, y0, right / w, y1]
        return _finalize([(rect_l, "_L"), (rect_r, "_R")], fmt, w, h)

    # 35mm (default for any other format string: half-frame | 35mm)
    if bbox is None:
        log.warning("35mm: frame bbox detection failed -> falling back to DEFAULT_CROP_35")
        return _finalize([(DEFAULT_CROP_35, "")], fmt, w, h)
    top, bottom, left, right = bbox
    rect = [left / w, top / h, right / w, bottom / h]
    return _finalize([(rect, "")], fmt, w, h)


# --- NegPy 0.62 half-frame auto-split (negpy/services/assets/half_frame.py) ---------
HALF_DETECT_NOTE = "_half_frame_detect"  # recipe note written by `negmcp new` on half-frame rolls


def detect_half_frame(arw_path) -> dict:
    """Run NegPy's own gutter + film-extent detector on one scan.

    Returns {"split_x", "gutter", "film_crop", "status"}; status is "ok", "failed" (no gutter:
    NegPy's (0.5, 0.0) fallback, or no film extent at all) or "partial" (some film edge not
    found -> that side would run to the scan border, i.e. into the holder)."""
    from negmcp import _negpy

    _negpy.bootstrap()
    from negpy.services.assets.half_frame import detect_split_and_crop_for_file

    split_x, gutter, film_crop = detect_split_and_crop_for_file(str(arw_path))
    rec = {"split_x": round(split_x, 5), "gutter": round(gutter, 5), "film_crop": None, "status": "ok"}
    if film_crop is not None:
        rec["film_crop"] = [round(v, 5) for v in film_crop]
    if (split_x, gutter) == (0.5, 0.0) or film_crop is None:
        rec["status"] = "failed"
    elif any(v in (0.0, 1.0) for v in film_crop):
        rec["status"] = "partial"
    return rec
