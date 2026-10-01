"""negmcp — FastMCP stdio server wrapping the NegPy film-negative renderer.

Tools (see docs/mcp-tools.md):
  render_frame   — render one ARW, return a JPEG preview + fingerprint text
  render_roll    — render all ARWs in a directory as a contact sheet
  analyze_frame  — NegPy normalization metrics (floors/ceils, metered_anchor, textural_range)
  fingerprint    — measure any image + compare to house_look.json
  write_sidecar  — write .negpy sidecars next to ARW files
  write_sidecars — all sidecars of a full-frame roll straight from its recipe

Run: ``negmcp serve`` (stdio).
"""

from __future__ import annotations

import io
import json
import logging
import math
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage
from PIL import Image, ImageDraw, ImageFont

from negmcp import recipe as recipe_mod
from negmcp import render as R
from negmcp._negpy import installed_version
from negmcp.config import get_config
from negmcp.exif import RollFormat
from negmcp.metrics import Fingerprint, fingerprint, open_srgb

__all__ = ["mcp", "serve"]

log = logging.getLogger(__name__)
CONTACT_CELL_PX = 600  # px per frame in the render_roll contact sheet

mcp = FastMCP("negmcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _downscale(img: Image.Image, long_edge: int) -> Image.Image:
    """Downscale to at most ``long_edge`` px on the longest side, preserving aspect."""
    w, h = img.size
    if max(w, h) <= long_edge:
        return img
    scale = long_edge / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _to_mcp_image(pil_img: Image.Image) -> MCPImage:
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=88, subsampling=0)
    return MCPImage(data=buf.getvalue(), format="jpeg")


def _fingerprint_text(fp: Fingerprint) -> str:
    m, s = fp["mid_wb"], fp["shadow_wb"]
    return (
        f"black={fp['black']:.3f}  white={fp['white']:.3f}  span={fp['span']:.3f}  "
        f"key={fp['key']:.3f}  mean={fp['mean']:.3f}  sat={fp['sat']:.3f}  "
        f"mid_wb=({m[0]:.3f},{m[1]:.3f},{m[2]:.3f})  shad=({s[0]:.3f},{s[1]:.3f},{s[2]:.3f})"
    )


HOUSE_AXES = ("black", "white", "span", "key", "sat", "gm", "yb")


def _load_srgb(path: Path) -> Image.Image:
    """sRGB pixels of an image, as ``negmcp qa`` reads them: a batch folder's ``_manifest.json``
    ``pixel_color_space`` wins (passthrough renders are working-space values deliberately
    tagged sRGB), else the embedded ICC."""
    manifest = path.parent / "_manifest.json"
    space = json.loads(manifest.read_text(encoding="utf-8")).get("pixel_color_space") if manifest.is_file() else None
    if space and space != "sRGB":
        with Image.open(path) as im:
            return R.to_srgb(im.convert("RGB"), space)
    return open_srgb(path)


def _house_comparison(fp: Fingerprint) -> list[str]:
    """Signed delta image - house_look median per axis; ``!`` = outside the refs p10..p90."""
    target_path = get_config().house_look
    if not target_path.exists():
        return [f"  house_look.json not found at {target_path}"]
    try:
        house = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"  house_look.json load error: {exc}"]
    tgt, bands = house.get("fingerprint", {}), house.get("bands", {})
    if not all(k in tgt for k in HOUSE_AXES):
        return ["  house_look.json has no negmcp fingerprint (old format): run `negmcp refs derive`"]
    target = "  ".join(f"{k}={tgt[k]:.3f}" for k in HOUSE_AXES)
    lines = [f"  target   : {target}  mid_wb={tgt.get('mid_wb')}  (median of {house.get('n_refs')} refs, sRGB)"]
    deltas = []
    for k in HOUSE_AXES:
        lo, hi = bands.get(k, (-math.inf, math.inf))
        mark = "!" if not lo <= fp[k] <= hi else ""  # type: ignore[literal-required]
        deltas.append(f"{k}={fp[k] - tgt[k]:+.3f}{mark}")  # type: ignore[literal-required]
    lines.append(f"  delta    : {'  '.join(deltas)}   (! = outside refs p10..p90)")
    return lines


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def render_frame(arw_path: str, config: dict, full_res: bool = False) -> list:
    """Render one film-negative ARW to a positive image.

    Iteration size by default: the cropped OUTPUT's long edge = iter_long_edge (2200 px),
    downscaled before the tone math (~2x faster than full res, fingerprint within ~0.01).
    ``full_res=True`` renders at full resolution.

    Returns a JPEG preview (preview_long_edge, default 1024 px) as MCP image content and a
    text line with the frame fingerprint (black/white point, span, key, saturation, mid-WB).
    The rendered JPEG is saved to ``<work_dir>/<stem>.jpg``.

    Args:
        arw_path: Absolute path to the .ARW file.
        config:   Flat NegPy config dict (a recipe ``base`` + overrides). Must carry
                  ``output_working_space`` (true/false), as every recipe base does.
        full_res: Render at full resolution instead of the iteration size.
    """
    cfg = get_config()
    arw = Path(arw_path).expanduser()
    img = R.render(str(arw), config, output_long_edge=None if full_res else cfg.iter_long_edge)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.work_dir / f"{arw.stem}.jpg"
    R.save_jpeg(img, out_path, quality=95, subsampling=0)
    fp_text = f"{arw.stem}: {_fingerprint_text(fingerprint(img))}  ({img.size[0]}x{img.size[1]} -> {out_path})"
    return [_to_mcp_image(_downscale(img, cfg.preview_long_edge)), fp_text]


@mcp.tool()
def render_roll(roll_dir: str, config: dict, per_frame: dict | None = None, recipe_path: str | None = None) -> list:
    """Render all ARWs in roll_dir (whole scan, no L/R split) into a labeled contact sheet.

    Frames render at preview_long_edge (1024 px) — a contact cell is 600 px. A preview, never
    a final: it does not stamp ``_graded_negpy_version`` (only ``negmcp batch --final`` does).

    Args:
        roll_dir:    Directory containing the ARW files.
        config:      Base flat NegPy config dict applied to all frames.
        per_frame:   Optional {frame_stem: {overrides}} to patch per-frame settings.
        recipe_path: Optional recipe the config came from; when it was graded on another
                     NegPy version the summary carries a warning.
    Returns:
        MCP image of the contact sheet + a summary text line.
    """
    cfg = get_config()
    rdir = Path(roll_dir).expanduser()
    arw_files = sorted(p for p in rdir.iterdir() if p.suffix.lower() == ".arw") if rdir.is_dir() else []
    if not arw_files:
        return [f"No .ARW files found in {rdir}"]

    per_frame = per_frame or {}
    cells: list[tuple[Image.Image, str]] = []
    for arw in arw_files:
        frame_cfg = {**config, **per_frame.get(arw.stem, {})}
        try:
            cells.append((R.render(str(arw), frame_cfg, output_long_edge=cfg.preview_long_edge), arw.stem))
        except Exception as exc:  # tool boundary: a broken frame becomes a red tile, not a failed call
            log.exception("render_roll: %s failed", arw.name)
            placeholder = Image.new("RGB", (CONTACT_CELL_PX, CONTACT_CELL_PX), (180, 60, 60))
            ImageDraw.Draw(placeholder).text((10, 10), f"ERR: {arw.stem}\n{exc}", fill=(255, 255, 255))
            cells.append((placeholder, arw.stem))

    n = len(cells)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    gap, label_h = 12, 28
    cell = CONTACT_CELL_PX
    sheet = Image.new("RGB", (cols * cell + (cols + 1) * gap, rows * (cell + label_h) + (rows + 1) * gap), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except OSError:
        font = ImageFont.load_default()

    for idx, (cell_img, stem) in enumerate(cells):
        x = gap + (idx % cols) * (cell + gap)
        y = gap + (idx // cols) * (cell + label_h + gap)
        thumb = cell_img.copy()
        thumb.thumbnail((cell, cell), Image.LANCZOS)
        sheet.paste(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2))
        draw.text((x + 4, y + cell + 4), stem[-30:], fill=(200, 200, 200), font=font)

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cs_path = cfg.work_dir / f"{rdir.name}_contact.jpg"
    sheet.save(cs_path, quality=90, subsampling=2)
    summary = f"Contact sheet: {n} frames from {rdir.name} -> {cs_path}"
    if recipe_path:
        drift = recipe_mod.engine_drift(recipe_mod.load_recipe(Path(recipe_path).expanduser()), installed_version())
        if drift:
            summary += f"\nWARNING: {drift}"
    return [_to_mcp_image(_downscale(sheet, 1800)), summary]


@mcp.tool()
def analyze_frame(arw_path: str, config: dict) -> str:
    """Run NegPy normalization and return exposure metrics for one frame.

    Reports: per-channel floors/ceils, metered_anchor, textural_range (+ every other
    scalar metric NegPy records). Useful for diagnosing exposure, cast, or clipping
    before committing a config.

    Args:
        arw_path: Absolute path to the .ARW file.
        config:   Flat NegPy config dict.
    """
    arw = Path(arw_path).expanduser()
    m = R.analyze_normalization(str(arw), config)
    lines = [f"analyze_frame: {arw.name}"]
    floors = m.get("floors") or m.get("log_floors")
    ceils = m.get("ceils") or m.get("log_ceils")
    if floors is not None:
        lines.append(f"  floors (R,G,B): {[round(float(v), 4) for v in floors]}")
    if ceils is not None:
        lines.append(f"  ceils  (R,G,B): {[round(float(v), 4) for v in ceils]}")
    if (anchor := m.get("metered_anchor")) is not None:
        lines.append(f"  metered_anchor: {round(float(anchor), 4)}")
    if (textural := m.get("textural_range")) is not None:
        lines.append(f"  textural_range: {round(float(textural), 4)}")
    known = {"floors", "ceils", "log_floors", "log_ceils", "metered_anchor", "textural_range", "geometry_params"}
    other_keys = sorted(set(m) - known)
    if other_keys:
        lines.append(f"  other metrics : {', '.join(other_keys)}")
        for k in other_keys:
            v = m[k]
            if isinstance(v, int | float | str | bool):
                lines.append(f"    {k}: {v}")
            elif isinstance(v, list | tuple) and len(v) <= 6:
                lines.append(f"    {k}: {[round(float(x), 4) if isinstance(x, int | float) else x for x in v]}")
    return "\n".join(lines)


@mcp.tool(name="fingerprint")
def fingerprint_tool(image_path: str) -> str:
    """Measure black/white point, span, key, saturation, midtone & shadow RGB of any image
    (converted to sRGB from its embedded ICC), and compare against house_look.json
    (config ``house_look``; derived from the refs by ``negmcp refs`` / ``negmcp start``).

    Args:
        image_path: Absolute path to a JPEG (or any PIL-readable) image.
    """
    path = Path(image_path).expanduser()
    fp = fingerprint(_load_srgb(path))
    return "\n".join([f"fingerprint: {path.name}", f"  measured : {_fingerprint_text(fp)}", *_house_comparison(fp)])


@mcp.tool()
def write_sidecar(roll_dir: str, configs: dict) -> str:
    """Write .negpy sidecar files next to each ARW in roll_dir.

    Uses NegPy's own WorkspaceConfig.to_dict() for serialization so the format matches
    exactly what NegPy reads back via load_sidecar / from_flat_dict.

    Args:
        roll_dir: Directory containing the ARW files.
        configs:  {frame_stem: flat_config_dict} — one entry per frame to write.
                  Frames absent from configs are skipped (not overwritten).
    """
    rdir = Path(roll_dir).expanduser()
    if not rdir.is_dir():
        return f"ERROR: {rdir} is not a directory"
    written, errors = [], []
    for stem, flat_cfg in configs.items():
        try:
            recipe_mod.write_sidecar_file(rdir, stem, flat_cfg)
            written.append(f"  {stem}.negpy")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"  {stem}: {exc}")
    lines = [f"write_sidecar: {rdir}"]
    if written:
        lines += [f"Written ({len(written)}):", *written]
    if errors:
        lines += [f"Errors ({len(errors)}):", *errors]
    if not written and not errors:
        lines.append("Nothing to write (configs dict was empty).")
    return "\n".join(lines)


@mcp.tool()
def write_sidecars(recipe_path: str, roll_dir: str | None = None, fmt: str | None = None) -> str:
    """Write a .negpy sidecar for every ARW of a FULL-FRAME roll from its recipe
    (base + per-frame override; recipe-skipped frames are not written).

    Half-frame rolls are refused with an error: NegPy keeps one sidecar (one crop) per
    ARW, so the L/R halves the batch renders cannot be represented.

    Args:
        recipe_path: Path to ``<roll>_recipe.json``.
        roll_dir:    Roll directory (default: neg_root/<roll> from the recipe file name).
        fmt:         "ff" | "half" to override the info.txt ``format:`` line.
    """
    try:
        written, skipped = recipe_mod.write_sidecars(
            Path(recipe_path).expanduser(),
            Path(roll_dir).expanduser() if roll_dir else None,
            RollFormat(fmt) if fmt else None,
        )
    except (recipe_mod.SidecarRefusedError, recipe_mod.RecipeError, ValueError, OSError) as exc:
        return f"ERROR: {exc}"
    lines = [f"write_sidecars: {len(written)} written"]
    lines += [f"  {p.name}" for p in written]
    if skipped:
        lines.append(f"skipped by recipe: {skipped}")
    return "\n".join(lines)


def serve() -> None:
    mcp.run(transport="stdio")
