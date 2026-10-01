"""Whole-roll batch: recipe -> review folder (the ``batch(recipe) == review`` invariant).

Format comes from the roll's info.txt ``format:`` line (override with ``--format``):
  * half: each ARW holds two half-frames -> ``<stem>_L.jpg`` / ``<stem>_R.jpg``, cropped with
    the per-side override's ``manual_crop_rect`` or crop.DEFAULT_CROP[side]. Near-black
    halves (mean luma < 0.06, film end / blank) are skipped.
  * ff:   one image per ARW -> ``<stem>.jpg``.

A per-frame override that is a string (half: per side) is a recipe skip; the JPG of a
skipped frame is removed from the review folder. Iteration runs render at
``iter_long_edge`` measured on the cropped OUTPUT (downscaled before the tone math);
``final`` renders full resolution and first clears the review folder's JPGs. Every run
writes ``_manifest.json`` (recipe hash, NegPy version/pin, the recipe's graded NegPy version,
output mode, size, frames). A full ``final`` run without errors stamps the recipe's
``_graded_negpy_version``; a run on a different engine than the stamp warns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from negmcp import _negpy, exif, render
from negmcp.config import Config
from negmcp.crop import DEFAULT_CROP
from negmcp.exif import RollFormat
from negmcp.metrics import LUMA
from negmcp.recipe import GRADED_VERSION_KEY, engine_drift, load_recipe, stamp_graded_version

__all__ = ["MANIFEST", "BatchResult", "build_tasks", "run_batch"]

log = logging.getLogger(__name__)
EMPTY_LUMA = 0.06
MANIFEST = "_manifest.json"


@dataclass(frozen=True, slots=True)
class Task:
    arw: Path
    config: dict
    out: Path
    exif: bytes
    long_edge: int | None  # output (cropped) long edge; None = full resolution
    check_empty: bool


@dataclass(slots=True)
class BatchResult:
    rendered: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # recipe skips + empty frames
    errors: list[str] = field(default_factory=list)  # render failures
    contact: Path | None = None
    manifest: Path | None = None
    negpy_metrics: dict[str, dict] = field(default_factory=dict)  # frame name -> NegPy meters
    engine_warning: str | None = None  # recipe graded on another NegPy than this render
    stamped: str | None = None  # NegPy version written to the recipe (full --final only)


def resolve_format(roll_dir: Path, override: str | None) -> RollFormat:
    if override:
        return RollFormat(override)
    fmt = exif.roll_format(exif.parse_info_txt(roll_dir / "info.txt"))
    if fmt is None:
        raise ValueError(f"{roll_dir}/info.txt has no usable `format:` line (half-frame / 35mm); pass --format half|ff")
    return fmt


def list_arws(roll_dir: Path) -> list[Path]:
    return sorted(p for p in roll_dir.iterdir() if p.is_file() and p.suffix.lower() == ".arw")


def build_tasks(
    roll_dir: Path,
    recipe: dict,
    fmt: RollFormat,
    out_dir: Path,
    long_edge: int | None,
    only: set[str] | None = None,
) -> tuple[list[Task], list[tuple[str, str]]]:
    """Tasks to render + (output name, reason) of frames the recipe skips."""
    info = exif.parse_info_txt(roll_dir / "info.txt")
    base = recipe["base"]
    pf = recipe.get("per_frame_overrides", {})
    tasks: list[Task] = []
    skipped: list[tuple[str, str]] = []
    for arw in list_arws(roll_dir):
        stem = arw.stem
        if only and stem not in only:
            continue
        ov = pf.get(stem, {})
        if fmt is RollFormat.HALF:
            exif_bytes = None
            for side in ("L", "R"):
                side_ov = ov.get(side) if isinstance(ov, dict) else ov
                name = f"{stem}_{side}.jpg"
                if isinstance(side_ov, str):
                    skipped.append((name, f"recipe: {side_ov}"))
                    continue
                cfg = {**base, "manual_crop_rect": DEFAULT_CROP[side], **(side_ov or {})}
                exif_bytes = exif_bytes or exif.batch_exif(info, arw)
                tasks.append(Task(arw, cfg, out_dir / name, exif_bytes, long_edge, True))
        else:
            name = f"{stem}.jpg"
            if isinstance(ov, str):
                skipped.append((name, f"recipe: {ov}"))
                continue
            cfg = {**base, **ov}
            tasks.append(Task(arw, cfg, out_dir / name, exif.batch_exif(info, arw), long_edge, False))
    return tasks, skipped


def _render_task(task: Task) -> tuple[str, Path | str, dict]:
    """Worker: render one frame and save it.
    Returns ("ok", path, negpy_metrics) | ("empty"|"error", reason, {})."""
    try:
        im = render.render(str(task.arw), task.config, output_long_edge=task.long_edge)
    except Exception as ex:  # worker boundary: one broken ARW must not take down the roll
        log.exception("render failed: %s", task.out.name)
        return ("error", f"{task.out.name}(err:{ex})", {})
    if task.check_empty:
        a = np.asarray(im.convert("RGB")).astype(np.float32) / 255.0
        if float((a @ LUMA).mean()) < EMPTY_LUMA:
            return ("empty", task.out.name, {})
    render.save_jpeg(im, task.out, quality=92, exif=task.exif)
    return ("ok", task.out, im.info.get("negpy_metrics", {}))


def _contact_sheet(paths: list[Path], cols: int, out: Path) -> Path:
    tw, gap, label_h = 300, 6, 14
    tiles = []
    for p in sorted(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((tw, tw), Image.LANCZOS)
        c = Image.new("RGB", (tw, tw + label_h), (20, 20, 20))
        c.paste(im, ((tw - im.size[0]) // 2, label_h))
        ImageDraw.Draw(c).text((3, 2), p.stem, fill=(225, 225, 225))
        tiles.append(c)
    rows = (len(tiles) + cols - 1) // cols
    th = tw + label_h
    sheet = Image.new("RGB", (cols * tw + (cols - 1) * gap, rows * th + (rows - 1) * gap), (20, 20, 20))
    for i, t in enumerate(tiles):
        r, cc = divmod(i, cols)
        sheet.paste(t, (cc * (tw + gap), r * (th + gap)))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=90)
    return out


def _clear_jpgs(out_dir: Path, only: set[str] | None) -> int:
    """--final: drop the JPGs about to be regenerated (all, or only the --only stems)."""
    n = 0
    for p in out_dir.glob("*.jpg"):
        if only is None or p.stem in only or p.stem.rsplit("_", 1)[0] in only:
            p.unlink()
            n += 1
    return n


def _write_manifest(
    out_dir: Path,
    recipe_path: Path,
    recipe: dict,
    fmt: RollFormat,
    long_edge: int | None,
    final: bool,
    only: set[str] | None,
    result: BatchResult,
) -> Path:
    pin = _negpy.read_pin()
    manifest = {
        "recipe": str(recipe_path),
        "recipe_sha256": hashlib.sha256(recipe_path.read_bytes()).hexdigest(),
        "negpy_version": _negpy.installed_version(),
        "negpy_pin": pin.commit,
        "graded_negpy_version": recipe.get(GRADED_VERSION_KEY),
        "engine_warning": result.engine_warning,
        "output_working_space": recipe["base"]["output_working_space"],
        "pixel_color_space": render.pixel_color_space(recipe["base"]),
        "format": fmt.value,
        "long_edge": long_edge,
        "final": final,
        "only": sorted(only) if only else None,
        "created_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "frames": sorted(p.name for p in result.rendered),
        "skipped": sorted(result.skipped),
        "errors": sorted(result.errors),
        "negpy_metrics": dict(sorted(result.negpy_metrics.items())),
    }
    path = out_dir / MANIFEST
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def run_batch(
    cfg: Config,
    roll: str,
    *,
    recipe_path: Path | None = None,
    fmt: str | None = None,
    final: bool = False,
    long_edge: int | None = None,
    only: set[str] | None = None,
    out_dir: Path | None = None,
) -> BatchResult:
    roll_dir = cfg.roll_dir(roll)
    recipe_path = recipe_path or cfg.recipe_path(roll_dir.name)
    out_dir = out_dir or cfg.review_dir(roll_dir.name)
    roll_fmt = resolve_format(roll_dir, fmt)
    recipe = load_recipe(recipe_path, fmt=roll_fmt)
    target = None if final else (long_edge or cfg.iter_long_edge)
    current = _negpy.installed_version()

    out_dir.mkdir(parents=True, exist_ok=True)
    tasks, recipe_skips = build_tasks(roll_dir, recipe, roll_fmt, out_dir, target, only)
    if final:
        log.info("final: removed %d JPGs from %s", _clear_jpgs(out_dir, only), out_dir)
    result = BatchResult(skipped=[f"{name}({why})" for name, why in recipe_skips])
    result.engine_warning = engine_drift(recipe, current)
    stale = [name for name, _ in recipe_skips]

    if tasks:
        ctx = mp.get_context("spawn")
        with ctx.Pool(min(cfg.workers, len(tasks))) as pool:
            outcomes = pool.map(_render_task, tasks)
        for kind, val, meters in outcomes:
            if kind == "ok":
                result.rendered.append(Path(val))
                result.negpy_metrics[Path(val).stem] = meters
            elif kind == "empty":
                result.skipped.append(f"{val}(empty)")
                stale.append(str(val))
            else:
                result.errors.append(str(val))
    else:
        log.error("no frames to render in %s (only=%s)", roll_dir, sorted(only or ()))

    for name in stale:
        (out_dir / name).unlink(missing_ok=True)
    if final and not only and result.rendered and not result.errors:
        stamp_graded_version(recipe_path, current)
        result.stamped = current
    result.manifest = _write_manifest(out_dir, recipe_path, recipe, roll_fmt, target, final, only, result)
    if not only and result.rendered:
        cols = 6 if roll_fmt is RollFormat.HALF else 3
        result.contact = _contact_sheet(result.rendered, cols, cfg.work_dir / f"{roll_dir.name}_review.jpg")
    return result
