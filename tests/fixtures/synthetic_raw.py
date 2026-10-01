"""A synthetic colour-negative scan as a minimal Bayer DNG (rawpy / LibRaw read it like a camera raw).

The scene is an orange-masked negative: a density ramp left -> right plus a dense
(= bright in the print) patch top-left. LibRaw detects the format from the content, so the
file may also be named ``*.ARW`` — that is how the roll tests feed it to the batch.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile

__all__ = ["make_negative_dng"]

MASK = np.array([0.80, 0.45, 0.25])  # orange-mask transmittance per channel
DYE = np.array([0.9, 1.0, 1.1])  # per-channel density response
BLACK, WHITE = 512, 16383


def _srational(values: list[float]) -> list[int]:
    out: list[int] = []
    for v in values:
        out += [round(v * 10000), 10000]
    return out


def make_negative_dng(path: Path, h: int = 400, w: int = 600, exposure: float = 0.0) -> Path:
    """Write the synthetic negative to ``path``; ``exposure`` shifts the whole density (stops-ish)."""
    _, x = np.mgrid[0:h, 0:w]
    dens = 0.2 + 0.7 * (x / w) + exposure
    dens[: h // 2, : w // 3] = 0.9 + exposure
    rgb = MASK[None, None, :] * 10 ** (-dens[..., None] * DYE)
    cfa = np.empty((h, w))
    cfa[0::2, 0::2] = rgb[0::2, 0::2, 0]  # R G
    cfa[0::2, 1::2] = rgb[0::2, 1::2, 1]  # G B
    cfa[1::2, 0::2] = rgb[1::2, 0::2, 1]
    cfa[1::2, 1::2] = rgb[1::2, 1::2, 2]
    data = (BLACK + np.clip(cfa, 0, 1) * (WHITE - BLACK)).astype(np.uint16)
    tags = [
        (254, "I", 1, 0, True),  # NewSubfileType: main image
        (33421, "H", 2, (2, 2), True),  # CFARepeatPatternDim
        (33422, "B", 4, (0, 1, 1, 2), True),  # CFAPattern: RGGB
        (50706, "B", 4, (1, 4, 0, 0), True),  # DNGVersion
        (50707, "B", 4, (1, 1, 0, 0), True),  # DNGBackwardVersion
        (50708, "s", 0, "negmcp synthetic", True),  # UniqueCameraModel
        (50714, "H", 1, BLACK, True),  # BlackLevel
        (50717, "H", 1, WHITE, True),  # WhiteLevel
        (50721, "2i", 9, _srational([1, 0, 0, 0, 1, 0, 0, 0, 1]), True),  # ColorMatrix1 (XYZ -> camera)
        (50728, "2I", 3, [10000, 10000] * 3, True),  # AsShotNeutral
        (50778, "H", 1, 21, True),  # CalibrationIlluminant1: D65
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, data, photometric=32803, compression=None, extratags=tags, metadata=None)
    return path
