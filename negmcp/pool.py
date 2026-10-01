"""Roll pooling (NegPy 0.62 "Roll Analysis", PR #1145/#1150) without Qt.

Per frame — the same steps as ``negpy/desktop/workers/render.py`` NormalizationWorker:
geometry + crop ROI -> ``resolve_analysis_region`` -> ``analyze_log_exposure_bounds`` (the
frame's own unmix and clip axes) -> ``measure_neutral_axis_from_log`` (C-41). Then
``pool_frame_bounds`` (luma-free colour outliers) and ``pool_neutral_axis``.

Written into a recipe, the result rides COLOUR and CAST only: ``use_color_average`` +
``use_cast_average`` on the pooled ``locked_floors/ceils/neutral_axis``, ``use_luma_average``
off — brightness and contrast stay per frame (the dead ``roll_lock`` lesson: a shared luma
baseline drags frames with different content). Outlier frames keep their own colour and cast
through a per-frame override, as the app does.

CLI: ``negmcp pool <roll> [--apply]``; the last result is cached at
``<work_dir>/pool/<roll>.json`` so ``negmcp qa`` can list outliers as fix candidates.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from negmcp.config import Config
from negmcp.exif import RollFormat

__all__ = ["POOL_KEYS", "FrameBounds", "PoolResult", "analyze_task", "apply_pool", "pool_cache_path", "pool_roll"]

ANALYSIS_LONG_EDGE = 1600  # NegPy's own preview size, the resolution the app's Roll Analysis meters at
POOL_KEYS = ("locked_floors", "locked_ceils", "locked_neutral_axis", "use_color_average", "use_cast_average")
OUTLIER_OVERRIDE = {"use_color_average": False, "use_cast_average": False}


@dataclass(frozen=True, slots=True)
class FrameBounds:
    name: str  # output frame name: <stem> (FF) or <stem>_<L|R> (half)
    floors: tuple[float, float, float]
    ceils: tuple[float, float, float]
    axis: tuple | None


@dataclass(slots=True)
class PoolResult:
    frames: list[FrameBounds]
    errors: list[str]
    floors: tuple[float, float, float]
    ceils: tuple[float, float, float]
    axis: tuple | None
    outliers: list[str]

    def base_update(self) -> dict:
        return {
            "locked_floors": list(self.floors),
            "locked_ceils": list(self.ceils),
            "locked_neutral_axis": _jsonable(self.axis),
            "use_luma_average": False,
            "use_color_average": True,
            "use_cast_average": self.axis is not None,
        }


def _jsonable(v):
    if isinstance(v, tuple | list | np.ndarray):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.floating | np.integer):
        return float(v)
    return v


def analyze_task(args: tuple[str, str, dict]) -> FrameBounds | str:
    """Worker: one frame's bounds + neutral axis, or an error string."""
    name, arw, flat = args
    try:
        from negpy.features.exposure.normalization import (
            analyze_log_exposure_bounds,
            effective_crosstalk_matrix,
            measure_neutral_axis_from_log,
            prefilter_log_grid,
            resolve_analysis_region,
            unmix_log_image,
        )
        from negpy.features.process.models import ProcessMode

        from negmcp import render

        prepared = render.prepare_frame(arw, flat, long_edge=ANALYSIS_LONG_EDGE)
        img, ctx = prepared["img"], prepared["context"]
        p = render.build_workspace_config(flat).process
        roi, buffer = resolve_analysis_region(img.shape, ctx.active_roi, p.analysis_buffer, p.analysis_rect)
        unmix = effective_crosstalk_matrix(p, p.process_mode)
        bounds = analyze_log_exposure_bounds(
            img,
            roi=roi,
            analysis_buffer=buffer,
            percentile_clip=p.luma_range_clip,
            color_clip=p.color_range_clip,
            unmix=unmix,
        )
        axis = None
        if p.process_mode == ProcessMode.C41:
            grid = unmix_log_image(prefilter_log_grid(img, roi, buffer), unmix)
            axis = measure_neutral_axis_from_log(grid, bounds, None, 0.0)
    except Exception as exc:  # worker boundary: one broken ARW must not abort the roll
        return f"{name}(err:{exc})"
    return FrameBounds(name, tuple(map(float, bounds.floors)), tuple(map(float, bounds.ceils)), axis)


def pool_frames(frames: list[FrameBounds], errors: list[str]) -> PoolResult:
    from negmcp import _negpy

    _negpy.bootstrap()
    from negpy.features.exposure.normalization import pool_frame_bounds, pool_neutral_axis

    if not frames:
        raise ValueError(f"no frame could be analysed: {errors}")
    pooled, mask = pool_frame_bounds(np.array([f.floors for f in frames]), np.array([f.ceils for f in frames]))
    axis = pool_neutral_axis([f.axis for f in frames], mask)
    return PoolResult(
        frames=frames,
        errors=errors,
        floors=tuple(map(float, pooled.floors)),
        ceils=tuple(map(float, pooled.ceils)),
        axis=axis,
        outliers=[f.name for f, out in zip(frames, mask, strict=True) if out],
    )


def pool_cache_path(cfg: Config, roll: str) -> Path:
    return cfg.work_dir / "pool" / f"{Path(roll).name}.json"


def pool_roll(cfg: Config, roll: str, recipe_path: Path | None = None, fmt: str | None = None) -> PoolResult:
    """Analyse every frame the batch would render (recipe skips excluded) and pool them."""
    from negmcp import _negpy
    from negmcp.batch import build_tasks, resolve_format
    from negmcp.recipe import load_recipe

    roll_dir = cfg.roll_dir(roll)
    roll_fmt = resolve_format(roll_dir, fmt)
    recipe = load_recipe(recipe_path or cfg.recipe_path(roll_dir.name), fmt=roll_fmt)
    # Analyse each frame WITHOUT any previous pool result, so re-pooling is idempotent.
    recipe = {**recipe, "base": {k: v for k, v in recipe["base"].items() if k not in POOL_KEYS}}
    tasks, _ = build_tasks(roll_dir, recipe, roll_fmt, cfg.work_dir, None)
    jobs = [(t.out.stem, str(t.arw), {k: v for k, v in t.config.items() if k not in POOL_KEYS}) for t in tasks]
    with mp.get_context("spawn").Pool(max(1, min(cfg.workers, len(jobs)))) as pool:
        out = pool.map(analyze_task, jobs)
    res = pool_frames([r for r in out if isinstance(r, FrameBounds)], [r for r in out if isinstance(r, str)])
    path = pool_cache_path(cfg, roll_dir.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "roll": roll_dir.name,
                "negpy_version": _negpy.installed_version(),
                "format": roll_fmt.value,
                "base_update": res.base_update(),
                "outliers": res.outliers,
                "errors": res.errors,
                "frames": {f.name: {"floors": f.floors, "ceils": f.ceils} for f in res.frames},
            },
            indent=1,
        )
        + "\n"
    )
    return res


def apply_pool(recipe_path: Path, res: PoolResult, fmt: RollFormat) -> None:
    """Write the pooled baseline into ``base`` and the outlier overrides per frame (one locked,
    atomic recipe write; earlier outlier overrides from a previous pool are cleared)."""
    from negmcp.recipe import _atomic_write_json, _merge_fix, _recipe_lock, load_recipe, validate_recipe

    with _recipe_lock(recipe_path):
        recipe = load_recipe(recipe_path, validate=False)
        recipe["base"].update(res.base_update())
        for entry in recipe.get("per_frame_overrides", {}).values():
            sides = entry.values() if isinstance(entry, dict) and set(entry) <= {"L", "R"} else [entry]
            for ov in sides:
                if isinstance(ov, dict):
                    for k in OUTLIER_OVERRIDE:
                        ov.pop(k, None)
        for name in res.outliers:
            stem, side = name.rsplit("_", 1) if fmt is RollFormat.HALF else (name, "FF")
            _merge_fix(recipe, stem, side, OUTLIER_OVERRIDE)
        validate_recipe(recipe, fmt=fmt, source=str(recipe_path))
        _atomic_write_json(recipe_path, recipe)
