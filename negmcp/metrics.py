"""The single image fingerprint: tone (black/white/span/key/mean), saturation, mid-WB.

Used by the MCP ``fingerprint``/``render_frame`` tools, the QA gate and the corridor
engine, so every number they print comes from the same masks and percentiles.

All values are measured on 8-bit RGB scaled to [0, 1], luma = Rec.709 weights:
  black / white   1st / 99th percentile of luma; span = white - black
  key / mean      median / mean luma
  sat             mean chroma (max - min) / max
  mid_wb          mean RGB of luma in [0.35, 0.65], normalised to mean 1 (>100 px, else 1,1,1)
  shadow_wb       same for luma in [0.05, 0.25]
  gm / yb         green-magenta / warm-cool axes of mid_wb: g-(r+b)/2, (r+g)/2-b
  clip / blown    fraction of luma > 0.97 / > 0.99
  midfrac         fraction of pixels in the mid-WB mask
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TypedDict

import numpy as np
from PIL import Image, ImageCms

__all__ = ["LUMA", "Fingerprint", "fingerprint", "fingerprint_delta", "open_srgb", "to_rgb01"]

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
SCALARS = ("black", "white", "span", "key", "mean", "sat", "gm", "yb", "clip", "blown")


class Fingerprint(TypedDict):
    black: float
    white: float
    span: float
    key: float
    mean: float
    sat: float
    mid_wb: list[float]
    shadow_wb: list[float]
    gm: float
    yb: float
    clip: float
    blown: float
    midfrac: float


def to_rgb01(img: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(img, Image.Image):
        return np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return np.asarray(img, dtype=np.float32)


_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def open_srgb(path: str | Path) -> Image.Image:
    """An image file as sRGB RGB: an embedded ICC (Display P3, Adobe RGB, ...) is converted
    (relative colorimetric); an untagged or sRGB-tagged file is taken as sRGB."""
    with Image.open(path) as im:
        icc = im.info.get("icc_profile")
        rgb = im.convert("RGB")
    if not icc:
        return rgb
    src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    if "srgb" in ImageCms.getProfileDescription(src).lower():
        return rgb
    return ImageCms.profileToProfile(
        rgb, src, _SRGB_PROFILE, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB"
    )


def _band_wb(rgb: np.ndarray, mask: np.ndarray) -> list[float]:
    if int(mask.sum()) <= 100:
        return [1.0, 1.0, 1.0]
    m = rgb[mask].mean(axis=0)
    return [float(v) for v in m / (m.mean() + 1e-9)]


def fingerprint(img: Image.Image | np.ndarray) -> Fingerprint:
    rgb = to_rgb01(img)
    lum = rgb @ LUMA
    p1, p50, p99 = (float(v) for v in np.percentile(lum, (1, 50, 99)))
    ch_max, ch_min = rgb.max(axis=2), rgb.min(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = float(np.where(ch_max > 0, (ch_max - ch_min) / ch_max, 0.0).mean())
    mid = (lum >= 0.35) & (lum <= 0.65)
    mid_wb = _band_wb(rgb, mid)
    r, g, b = mid_wb
    return Fingerprint(
        black=p1,
        white=p99,
        span=p99 - p1,
        key=p50,
        mean=float(lum.mean()),
        sat=sat,
        mid_wb=mid_wb,
        shadow_wb=_band_wb(rgb, (lum >= 0.05) & (lum <= 0.25)),
        gm=g - (r + b) / 2.0,
        yb=(r + g) / 2.0 - b,
        clip=float((lum > 0.97).mean()),
        blown=float((lum > 0.99).mean()),
        midfrac=float(mid.mean()),
    )


def fingerprint_delta(a: Fingerprint, b: Fingerprint) -> dict[str, float]:
    """|a - b| per scalar axis plus the max mid-WB channel difference."""
    out = {k: abs(float(a[k]) - float(b[k])) for k in SCALARS}  # type: ignore[literal-required]
    out["mid_wb"] = max(abs(x - y) for x, y in zip(a["mid_wb"], b["mid_wb"], strict=True))
    return out
