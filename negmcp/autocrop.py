"""Roll autocrop through NegPy's own detector — the app's "Auto Crop All", headless.

Upstream does the work; negmcp only feeds it frames and writes the answer into the recipe:

    decode      PreviewManager.load_linear_preview, as the plain-frame branch of
                desktop/workers/render.py ``decode_asset_preview`` (preview size, preview
                demosaic, camera-WB / highlight / lens gates from the frame's config)
    half-frame  each ARW is cut into its halves with ``half_frame.slice_for_asset`` on the
                (split_x, gutter, film_crop) of ``detect_split_and_crop_for_file`` — the profile
                the app's "Auto-detect All Splits" saves for a half-frame roll
    detect      flat-field + GeometryProcessor with the crop cleared (BatchAutoCropWorker
                ``_frame_evidence``) -> ``batch_autocrop.detect_crop_candidate`` per frame
    resolve     ``batch_autocrop.resolve_roll_crops`` over the whole roll (template from the
                trusted frames, safety border; ambiguous frames abstain)
    write-back  as ``controller._on_batch_autocrop_finished``: ``crop_rect`` (our recipes spell
                it ``manual_crop_rect``, migrated by NegPy) + ``fine_rotation += correction_angle``

The BatchAutoCropWorker itself is a PyQt6 QObject (not importable headless), so its ~20 lines
of per-frame glue are mirrored here, nothing more.

A half is a portrait canvas, which ``detect_crop_candidate`` keeps out of the roll template:
it gets NegPy's single-frame crop (``get_autocrop_coords``), exactly as in the app. Its rect is
half-local; ``half_frame._to_scan`` maps it to the full-scan fractions our L/R
``manual_crop_rect`` are in.

Frames whose recipe override already carries a crop are preserved (the app's
``has_manual_crop`` preflight) unless ``force``; a crop in the recipe ``base`` is a roll default
and does not preserve anything.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from negmcp import _negpy, render
from negmcp.exif import RollFormat

_negpy.bootstrap()

from negpy.domain.interfaces import PipelineContext
from negpy.features.flatfield.logic import apply_flatfield
from negpy.features.geometry.batch_autocrop import CropEvidence, detect_crop_candidate, resolve_roll_crops
from negpy.features.geometry.models import FINE_ROTATION_LIMIT, GeometryConfig
from negpy.features.geometry.processor import GeometryProcessor
from negpy.features.process.logic import (
    effective_highlight_reconstruction,
    effective_linear_raw,
    highlight_reconstruction_bakes_wb,
)
from negpy.infrastructure.display.color_spaces import WORKING_COLOR_SPACE
from negpy.services.assets.half_frame import HalfGeometry, _to_scan, detect_split_and_crop_for_file, slice_for_asset
from negpy.services.rendering.lens import metadata_lens_corrections
from negpy.services.rendering.preview_manager import PreviewManager

__all__ = ["AutocropResult", "FrameCrop", "autocrop_roll", "recipe_fixes"]

log = logging.getLogger(__name__)

HALVES = {"L": 1, "R": 2}  # slice_half: half 1 = left, 2 = right
CROP_KEYS = ("manual_crop_rect", "crop_rect")
_IDENTITY_GEOMETRY = GeometryConfig()


@dataclass(frozen=True, slots=True)
class FrameCrop:
    """One resolved frame. ``rect`` is in the fractions ``manual_crop_rect`` is written in (the
    frame's transformed full scan); ``fine_rotation`` is the value to write (existing +
    NegPy's correction), None when the correction is zero."""

    stem: str
    side: str  # "FF" | "L" | "R"
    rect: tuple[float, float, float, float]
    fine_rotation: float | None
    correction_angle: float
    confidence: float
    calibrated: bool

    @property
    def name(self) -> str:
        return self.stem if self.side == "FF" else f"{self.stem}_{self.side}"


@dataclass(slots=True)
class AutocropResult:
    fmt: RollFormat
    frames: list[FrameCrop] = field(default_factory=list)
    unresolved: dict[str, str] = field(default_factory=dict)  # frame name -> why NegPy abstained
    preserved: list[str] = field(default_factory=list)  # frames whose override already has a crop
    skipped: list[str] = field(default_factory=list)  # recipe skips
    errors: dict[str, str] = field(default_factory=dict)  # ARW stem -> decode/detect failure
    splits: dict[str, dict] = field(default_factory=dict)  # half-frame: stem -> NegPy's split profile


@dataclass(frozen=True, slots=True)
class _Job:
    arw: Path
    sides: dict[str, dict]  # side ("FF" | "L" | "R") -> flat config of that frame


def _has_crop(override: object) -> bool:
    return isinstance(override, Mapping) and any(override.get(k) for k in CROP_KEYS)


def _jobs(roll_dir: Path, recipe: Mapping, fmt: RollFormat, force: bool, res: AutocropResult) -> list[_Job]:
    base = recipe["base"]
    pf = recipe.get("per_frame_overrides", {})
    jobs = []
    for arw in sorted(p for p in roll_dir.iterdir() if p.is_file() and p.suffix.lower() == ".arw"):
        ov = pf.get(arw.stem, {})
        entries = (
            {s: (ov.get(s, {}) if isinstance(ov, Mapping) else ov) for s in HALVES}
            if fmt is RollFormat.HALF
            else {"FF": ov}
        )
        sides = {}
        for side, entry in entries.items():
            name = arw.stem if side == "FF" else f"{arw.stem}_{side}"
            if isinstance(entry, str):
                res.skipped.append(name)
            elif _has_crop(entry) and not force:
                res.preserved.append(name)
            else:
                sides[side] = {k: v for k, v in {**base, **(entry or {})}.items() if k not in CROP_KEYS}
        if sides:
            jobs.append(_Job(arw, sides))
    return jobs


def _decode(pm: PreviewManager, arw: Path, cfg) -> object:
    """The plain-frame branch of NegPy's ``decode_asset_preview``."""
    raw, _, _ = pm.load_linear_preview(
        str(arw),
        WORKING_COLOR_SPACE,
        use_camera_wb=not effective_linear_raw(cfg.process),
        full_resolution=False,
        file_hash=None,
        demosaic=cfg.process.demosaic_preview,
        positive_source=cfg.process.positive_source,
        highlight_mode=effective_highlight_reconstruction(cfg.process),
        bake_camera_wb=highlight_reconstruction_bakes_wb(cfg.process),
        lens_corrections=metadata_lens_corrections(cfg),
        lens_flatfield=cfg.flatfield,
    )
    return raw


def _evidence(key: str, raw, cfg) -> CropEvidence:
    """BatchAutoCropWorker._frame_evidence after the decode."""
    corrected = raw if metadata_lens_corrections(cfg) else apply_flatfield(raw, cfg.flatfield)
    geometry = replace(cfg.geometry, crop_rect=None, crop_from_auto=False, autocrop_offset=0)
    context = PipelineContext(
        original_size=(corrected.shape[1], corrected.shape[0]), scale_factor=1.0, process_mode=cfg.process.process_mode
    )
    transformed = GeometryProcessor(geometry).process(corrected, context)
    return detect_crop_candidate(
        key, transformed, target_ratio=cfg.geometry.autocrop_ratio, rebate_trim=cfg.geometry.autocrop_rebate_trim
    )


def _half_geometry(geo: GeometryConfig) -> bool:
    """A half's own geometry must be the identity: its rect is mapped onto the full scan, and a
    rotation / flip / keystone of the half has no full-scan counterpart."""
    keep = ("autocrop_offset", "autocrop_ratio", "autocrop_mode", "autocrop_rebate_trim")
    return replace(geo, crop_rect=None, crop_from_auto=False, **{k: getattr(_IDENTITY_GEOMETRY, k) for k in keep}) == (
        _IDENTITY_GEOMETRY
    )


def _frame_job(
    pm: PreviewManager, job: _Job, fmt: RollFormat
) -> tuple[list[CropEvidence], dict | None, dict[str, str]]:
    """Evidence for every frame of one ARW (+ the half-frame split profile, + per-frame refusals)."""
    cfgs = {side: render.build_workspace_config(flat) for side, flat in job.sides.items()}
    refused: dict[str, str] = {}
    if fmt is RollFormat.FF:
        cfg = cfgs["FF"]
        return [_evidence(job.arw.stem, _decode(pm, job.arw, cfg), cfg)], None, refused
    split_x, gutter, film_crop = detect_split_and_crop_for_file(str(job.arw))
    split = {"split_x": split_x, "gutter_thickness": gutter, "crop_rect": film_crop}
    evidence = []
    raw = None
    for side, cfg in cfgs.items():
        name = f"{job.arw.stem}_{side}"
        if not _half_geometry(cfg.geometry):
            refused[name] = "half-frame side has its own rotation/flip/keystone: no full-scan rect for it"
            continue
        raw = raw if raw is not None else _decode(pm, job.arw, cfg)
        evidence.append(_evidence(name, slice_for_asset(raw, {"half": HALVES[side], **split}), cfg))
    return evidence, split, refused


def _to_full_scan(rect, side: str, split: dict) -> tuple[float, float, float, float]:
    geom = HalfGeometry(
        crop_rect=tuple(split["crop_rect"]) if split["crop_rect"] is not None else None,
        split_x=split["split_x"],
        gutter_thickness=split["gutter_thickness"],
    )
    x1, y1 = _to_scan(rect[0], rect[1], HALVES[side], geom)
    x2, y2 = _to_scan(rect[2], rect[3], HALVES[side], geom)
    return (x1, y1, x2, y2)


def autocrop_roll(
    roll_dir: Path, recipe: Mapping, fmt: RollFormat, *, force: bool = False, workers: int = 1
) -> AutocropResult:
    """NegPy's roll autocrop over every frame of ``roll_dir`` under ``recipe`` (read only)."""
    res = AutocropResult(fmt)
    jobs = _jobs(roll_dir, recipe, fmt, force, res)
    pm = PreviewManager()

    def run(job: _Job):
        try:
            return job, _frame_job(pm, job, fmt)
        except Exception as ex:  # worker boundary: one broken ARW must not take down the roll
            log.exception("autocrop detection failed: %s", job.arw.name)
            return job, ex

    # The app runs half its workers: each holds a preview-size frame and the detector is multi-core.
    with ThreadPoolExecutor(max_workers=max(1, min(workers // 2, len(jobs) or 1))) as ex:
        outcomes = list(ex.map(run, jobs))

    evidence: list[CropEvidence] = []
    fine: dict[str, float] = {}
    for job, out in outcomes:
        if isinstance(out, Exception):
            res.errors[job.arw.stem] = f"{type(out).__name__}: {out}"
            continue
        ev, split, refused = out
        evidence += ev
        res.unresolved.update(refused)
        if split is not None:
            res.splits[job.arw.stem] = split
        for side, flat in job.sides.items():
            fine[job.arw.stem if side == "FF" else f"{job.arw.stem}_{side}"] = float(flat.get("fine_rotation", 0.0))

    by_key = {item.key: item for item in evidence}
    for crop in resolve_roll_crops(evidence):
        stem, _, side = crop.key.rpartition("_") if fmt is RollFormat.HALF else (crop.key, "", "FF")
        rect = crop.crop_rect
        angle = float(crop.correction_angle)
        if side != "FF":
            if abs(angle) > 1e-4:
                res.unresolved[crop.key] = f"deskew {angle:+.2f} deg on a half: fine_rotation would turn the whole scan"
                continue
            rect = _to_full_scan(rect, side, res.splits[stem])
        new_fine = fine[crop.key] + angle
        if abs(new_fine) > FINE_ROTATION_LIMIT:
            res.unresolved[crop.key] = f"fine_rotation {new_fine:+.2f} past NegPy's +/-{FINE_ROTATION_LIMIT}"
            continue
        res.frames.append(
            FrameCrop(
                stem=stem,
                side=side,
                rect=tuple(round(float(v), 5) for v in rect),
                fine_rotation=round(new_fine, 4) if abs(angle) > 1e-4 else None,
                correction_angle=round(angle, 4),
                confidence=round(float(crop.confidence), 3),
                calibrated=bool(crop.calibrated),
            )
        )
    done = {f.name for f in res.frames}
    for key, item in by_key.items():
        if key not in done and key not in res.unresolved:
            res.unresolved[key] = item.reason or "not placeable by the roll template"
    res.frames.sort(key=lambda f: f.name)
    return res


def recipe_fixes(res: AutocropResult) -> list[tuple[str, str, dict]]:
    """(stem, side, overrides) to merge into the recipe for every resolved frame."""
    fixes = []
    for f in res.frames:
        ov: dict = {"manual_crop_rect": list(f.rect)}
        if f.fine_rotation is not None:
            ov["fine_rotation"] = f.fine_rotation
        fixes.append((f.stem, f.side, ov))
    return fixes
