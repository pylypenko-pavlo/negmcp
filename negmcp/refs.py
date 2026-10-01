"""The refs set (``refs_dir``) measured ONCE -> both refs-derived targets.

  corridor.json    (<work_dir>/corridor/)  per-axis p10..p90 bands, what ``negmcp qa`` checks
  house_look.json  (config ``house_look``) median fingerprint + bands, what the MCP
                   ``fingerprint`` tool compares an image to; plus the user's hand-written
                   fields (``taste`` ...), which are kept verbatim
  refs_contact.jpg (<work_dir>/)           the refs at a glance

Every ref is read through ``metrics.open_srgb`` (embedded ICC -> sRGB) and measured with
``metrics.fingerprint`` — the same numbers QA and the MCP tools print. Each target carries
the ``refs_hash`` (``corridor.refs_fingerprint``: names + sizes + mtimes) it was built
from; a target whose hash differs from the current refs set is stale. ``ensure`` measures
the refs only when a target is stale (or forced) and then rewrites the stale ones.

CLI: ``negmcp refs check`` (re-derive what is stale) | ``negmcp refs derive`` (force both).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from negmcp import corridor, metrics
from negmcp.config import get_config

__all__ = ["DERIVED_KEYS", "RefsStatus", "derive_house", "ensure", "measure_refs", "summary"]

# house_look.json keys this module owns; anything else is hand-written and survives a derive.
DERIVED_KEYS = ("_note", "n_refs", "refs_hash", "measure", "space", "derived_at", "fingerprint", "bands")
# Bump when the per-ref measurement changes: a target built by another measure is stale even
# if the refs did not change (corridor.json is per machine, house_look.json lives in look_dir).
MEASURE = "metrics.fingerprint/srgb-icc/1"
# refs_look.py (v1) keys in v1 units (colourist temp/tint, R/G/B ratios on relative-luma masks):
# dropped on the first derive, nothing reads them.
LEGACY_KEYS = ("vinci_baseline_guidance",)
HOUSE_SCALARS = ("black", "white", "span", "key", "mean", "sat", "gm", "yb")
BAND_AXES = ("black", "white", "span", "key", "sat", "gm", "yb")
TILE = 360
NOTE = (
    "refs-derived fields (fingerprint, bands, n_refs, refs_hash) are written by `negmcp refs` / "
    "`negmcp start` from refs_dir: median / p10..p90 over the refs of negmcp.metrics.fingerprint "
    "(sRGB; the same measure as the QA corridor). Other keys (taste, ...) are hand-written and kept."
)


@dataclass(slots=True)
class RefsStatus:
    count: int
    refs_hash: str
    corridor: dict
    corridor_why: str | None  # None = was current
    house: dict | None  # None when house_look.json's folder does not exist
    house_why: str | None
    contact: Path | None


def measure_refs(refs_dir: Path) -> tuple[list[tuple[str, metrics.Fingerprint]], list[Image.Image]]:
    """(file, fingerprint) per readable ref + labelled thumbnails; unreadable files are skipped."""
    rows, tiles = [], []
    for f in sorted(f for f in os.listdir(refs_dir) if f.lower().endswith(corridor.REF_EXTS)):
        try:
            im = metrics.open_srgb(refs_dir / f)
        except OSError as exc:
            print(f"  [skip] {f}: {exc!r}")
            continue
        rows.append((f, metrics.fingerprint(im)))
        tiles.append(_tile(im, f))
    return rows, tiles


def _tile(im: Image.Image, name: str) -> Image.Image:
    t = im.copy()
    t.thumbnail((TILE, TILE), Image.Resampling.LANCZOS)
    cell = Image.new("RGB", (TILE, TILE + 16), (20, 20, 20))
    cell.paste(t, ((TILE - t.size[0]) // 2, 16 + (TILE - t.size[1]) // 2))
    ImageDraw.Draw(cell).text((4, 3), name[:28], fill=(230, 230, 230))
    return cell


def _contact(tiles: list[Image.Image], out: Path, cols: int = 5, gap: int = 8) -> Path:
    rows = (len(tiles) + cols - 1) // cols
    th = TILE + 16
    sheet = Image.new("RGB", (cols * TILE + (cols - 1) * gap, rows * th + (rows - 1) * gap), (20, 20, 20))
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet.paste(t, (c * (TILE + gap), r * (th + gap)))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return out


def derive_house(fps: list[metrics.Fingerprint], refs_hash: str, old: dict | None) -> dict:
    """house_look from the refs' fingerprints; the hand-written keys of ``old`` are kept, in order."""
    med = {k: round(float(np.median([f[k] for f in fps])), 4) for k in HOUSE_SCALARS}
    for k in ("mid_wb", "shadow_wb"):
        med[k] = [round(float(np.median([f[k][i] for f in fps])), 4) for i in range(3)]
    bands = {k: [round(float(np.percentile([f[k] for f in fps], q)), 4) for q in (10, 90)] for k in BAND_AXES}
    manual = {k: v for k, v in (old or {}).items() if k not in DERIVED_KEYS and k not in LEGACY_KEYS}
    return {
        "_note": NOTE,
        **manual,
        "n_refs": len(fps),
        "refs_hash": refs_hash,
        "measure": MEASURE,
        "space": "sRGB",
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fingerprint": med,
        "bands": bands,
    }


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _why(current: dict | None, refs_hash: str, force: bool) -> str | None:
    if force:
        return "forced"
    if current is None:
        return "missing"
    if current.get("refs_hash") is None:
        return "not derived yet"
    if current.get("refs_hash") != refs_hash:
        return "refs changed"
    return "measure changed" if current.get("measure") != MEASURE else None


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ensure(force: bool = False) -> RefsStatus:
    """Bring corridor.json and house_look.json up to the current refs set (one measurement
    pass for both). Raises OSError when refs_dir is missing."""
    cfg = get_config()
    count, refs_hash = corridor.refs_fingerprint(cfg.refs_dir)
    corr = corridor.load_corridor()
    house_path = cfg.house_look
    house_dir_ok = house_path.parent.is_dir()
    house = _read_json(house_path) if house_dir_ok else None
    corridor_why = _why(corr, refs_hash, force)
    house_why = _why(house, refs_hash, force) if house_dir_ok else None
    contact = None
    if corridor_why or house_why:
        rows, tiles = measure_refs(cfg.refs_dir)
        if not rows:
            raise OSError(f"no readable refs in {cfg.refs_dir}")
        if corridor_why:
            corr = corridor.calibrate([dict(file=f, **corridor.measure_fp(fp)) for f, fp in rows], refs_hash, MEASURE)
        if house_why:
            house = derive_house([fp for _, fp in rows], refs_hash, house)
            _write_atomic(house_path, house)
        contact = _contact(tiles, cfg.work_dir / "refs_contact.jpg")
    return RefsStatus(count, refs_hash, corr, corridor_why, house, house_why, contact)


def summary(st: RefsStatus) -> str:
    def part(name: str, why: str | None) -> str:
        return f"{name} re-derived ({why})" if why else f"{name} up to date"

    house = part("house_look", st.house_why) if st.house is not None else "house_look: folder missing, skipped"
    return f"{st.count} images, hash {st.refs_hash[:12]}: {part('corridor', st.corridor_why)}; {house}"
