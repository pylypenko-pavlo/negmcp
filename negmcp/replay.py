"""Faithful per-frame replay of the NegPy desktop app's edits.db config.

For rolls already graded frame by frame in the NegPy app, reproduce that app config per
ARW via render() instead of the recipe pipeline, which would flatten intentionally
different per-frame grades into one averaged base.

Match: file_hash (NegPy's calculate_file_hash: size + head/tail sha256) ->
file_settings.settings_json, which IS a full flat NegPy config (wb/grade/crosstalk/
manual_crop_rect/rotation/...). from_flat_dict drops the export/UI keys it doesn't use.

Render via render() (NOT render_crops): the app's crop_rect / rotation / fine_rotation
already encode the framing, and NegPy's engine applies them exactly as the app does.

Output colour: the app's own export settings (``export_color_space`` from edits.db,
colour-managed like the app's export: ``output_working_space=False``); an sRGB export
compares best with a colour-managed render, a "Same as Source" export is the working space
either way. ``working_space=True`` writes working-space values tagged with the target (see
render._to_pil), which matches exports made by pre-0.53 NegPy builds.

Frames with no edits.db hash match render via an optional fallback recipe's base and
are reported as "fallback" — never dropped silently.

Per-frame override layer (one-off fixes on top of the app config): optional
``<roll>_overrides.json`` ``{stem: {field: value}}``, looked up as the explicit path, then
``<roll_dir>/<roll>_overrides.json``, then ``<rolls_dir>/<roll>_overrides.json``.
Applied after the app/fallback config is picked, so it always wins.

CLI: ``negmcp replay <roll> [--fallback-recipe R] [--overrides O] [--out DIR] [--long-edge N] [--working-space]``
(edits.db path = config ``edits_db``, opened read-only; ``--long-edge`` = iteration size of
the OUTPUT frame, default full resolution).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from negmcp import exif, render
from negmcp.config import Config

__all__ = ["ReplayResult", "run_replay"]


@dataclass(slots=True)
class ReplayResult:
    replayed: list[Path] = field(default_factory=list)
    fallback: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    overrides_for: list[str] = field(default_factory=list)
    total: int = 0


def _resolve_overrides_path(cfg: Config, roll_dir: Path, explicit: Path | None) -> Path | None:
    if explicit:
        return explicit
    name = f"{roll_dir.name}_overrides.json"
    for c in (roll_dir / name, cfg.rolls_dir / name):
        if c.exists():
            return c
    return None


def load_edits(db_path: Path) -> dict[str, dict]:
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute("select file_hash, settings_json from file_settings").fetchall()
    finally:
        con.close()
    return {h: json.loads(sj) for h, sj in rows}


def _render_one(task: tuple[str, dict, str, bool, str, int | None]) -> tuple[str, str]:
    arw, cfg, outp, is_fallback, roll_dir, long_edge = task
    try:
        im = render.render(arw, cfg, output_long_edge=long_edge)
    except Exception as ex:  # worker boundary: one broken ARW must not take down the roll
        return ("skip", f"{Path(outp).name}(err:{ex})")
    render.save_jpeg(im, Path(outp), quality=95, subsampling=0)
    exif.tag_jpeg(outp, roll_dir, arw_path=arw)
    return ("fallback" if is_fallback else "replayed", outp)


def run_replay(
    cfg: Config,
    roll: str,
    *,
    fallback_recipe: Path | None = None,
    overrides_path: Path | None = None,
    out_dir: Path | None = None,
    long_edge: int | None = None,
    working_space: bool = False,
) -> ReplayResult:
    from negpy.kernel.image.logic import calculate_file_hash  # importable only after render bootstrapped the pin

    roll_dir = cfg.roll_dir(roll)
    out_dir = out_dir or cfg.review_dir(roll_dir.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    edits = load_edits(cfg.edits_db)
    ov_path = _resolve_overrides_path(cfg, roll_dir, overrides_path)
    overrides = json.loads(ov_path.read_text()) if ov_path and ov_path.exists() else {}
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    arws = sorted(p for p in roll_dir.iterdir() if p.suffix.lower() == ".arw")
    result = ReplayResult(overrides_for=sorted(overrides), total=len(arws))
    if not arws:
        raise FileNotFoundError(f"no ARWs in {roll_dir}")

    fallback_cfg = None
    if fallback_recipe:
        recipe = json.loads(fallback_recipe.read_text())
        fallback_cfg = recipe.get("base", recipe)

    tasks = []
    for p in arws:
        app_cfg = edits.get(calculate_file_hash(str(p)))
        is_fallback = app_cfg is None
        if is_fallback:
            result.unmatched.append(p.stem)
            if fallback_cfg is None:
                continue
            frame_cfg = dict(fallback_cfg)
        else:
            frame_cfg = dict(app_cfg)
        frame_cfg.update(overrides.get(p.stem, {}))
        frame_cfg["output_working_space"] = working_space
        tasks.append((str(p), frame_cfg, str(out_dir / f"{p.stem}.jpg"), is_fallback, str(roll_dir), long_edge))

    if not tasks:
        return result
    ctx = mp.get_context("spawn")
    with ctx.Pool(min(cfg.workers, len(tasks))) as pool:
        outcomes = pool.map(_render_one, tasks)
    for kind, val in outcomes:
        if kind == "replayed":
            result.replayed.append(Path(val))
        elif kind == "fallback":
            result.fallback.append(Path(val))
        else:
            result.skipped.append(val)
    return result
