"""NegPy render glue: ARW -> positive PIL image through NegPy's OWN engine, headless.

negmcp builds a ``WorkspaceConfig`` from a flat recipe config and hands the frame to NegPy's
``ImageProcessor`` on the CPU (``use_gpu=False``; no Qt is imported). Everything between the
RAW and the float buffer is upstream code, so it cannot drift from the app on a pin move:

    ImageProcessor._load_source_f32    decode (libraw params, WB / highlight gates, user_sat,
                                       EXIF orientation, flat-field, sensor un-mix) + the
                                       camera matrix / as-shot WB memo the slide path needs
    ImageProcessor.run_pipeline        dust/IR bakes, armed auto-crop, DarkroomEngine.process:
                                       geometry (rot/flip/fine rotation/distortion/keystone),
                                       base + exposure per render path (print: C-41/B&W,
                                       transfer: E-6), hue trim, CLAHE, Lab, lith/cyanotype,
                                       toning, crop, finish, working-space OETF
    ImageProcessor._apply_scaling_and_border_f32   export paper layout
    ImageProcessor.buffer_to_pil / _export_pixels  8-bit + colour management + ICC bytes

negmcp only adds what the app does not have:
  * iteration sizing — the source is shrunk BEFORE the pipeline with NegPy's own
    ``_downsample_to_long_edge`` (the contact-sheet path, ``render_display_array``), sized so
    the CROPPED output lands at ~``output_long_edge`` (half-frame aware);
  * ``output_working_space`` — passthrough mode: the working-space buffer written as-is and
    tagged with the target profile (see :func:`_to_pil`);
  * crosstalk profiles named by file stem as well as by display name;
  * ``render_crops`` — format-aware rebate detection for frames without a manual crop.

Engine version: the pin in vendor/NEGPY_PIN (enforced by negmcp._negpy.bootstrap).
"""

import dataclasses
import logging
import math
import os
import typing
from functools import cache

import numpy as np
from PIL import Image

from negmcp import _negpy, crop

_negpy.bootstrap()

from negpy.domain.interfaces import PipelineContext
from negpy.domain.migrations import migrate_flat_config
from negpy.domain.models import ColorSpace, ExportResolutionMode, WorkspaceConfig
from negpy.features.geometry.processor import GeometryProcessor
from negpy.features.process.capture_color import wb_only_cam_xyz
from negpy.infrastructure.display.color_spaces import WORKING_COLOR_SPACE
from negpy.kernel.system.config import APP_CONFIG
from negpy.kernel.system.paths import get_resource_path
from negpy.services.assets.crosstalk import CrosstalkProfiles
from negpy.services.rendering.engine import base_processor
from negpy.services.rendering.image_processor import ImageProcessor, _downsample_to_long_edge

log = logging.getLogger(__name__)

# Keys negmcp itself reads from a flat config; stripped before NegPy's from_flat_dict sees
# the dict (it would warn "Dropping unknown config keys" on every render).
#   output_working_space  required, bool: True = working-space passthrough,
#                         False = colour-managed export (see _to_pil)
#   crop_inset            optional per-frame tightening of render_crops' detected rect
OWN_KEYS = frozenset({"output_working_space", "crop_inset"})


class MissingOutputModeError(ValueError):
    """A flat config without ``output_working_space`` — the colour mode must be explicit."""


def output_working_space(flat_config: dict) -> bool:
    """The output colour mode, taken ONLY from the config (recipe ``base``); no env switch."""
    try:
        value = flat_config["output_working_space"]
    except KeyError:
        raise MissingOutputModeError(
            "flat config has no 'output_working_space': set it in the recipe base "
            "(true = working-space passthrough, false = colour-managed export)"
        ) from None
    if not isinstance(value, bool):
        raise MissingOutputModeError(f"'output_working_space' must be true/false, got {value!r}")
    return value


@cache
def known_config_keys() -> frozenset[str]:
    """Every flat key NegPy's from_flat_dict keeps (fields of the WorkspaceConfig sections,
    after migrations), plus ``local_masks`` and negmcp's OWN_KEYS."""
    hints = typing.get_type_hints(WorkspaceConfig)
    keys = {"local_masks"}
    for f in dataclasses.fields(WorkspaceConfig):
        section = hints[f.name]
        if f.name != "local" and dataclasses.is_dataclass(section):
            keys.update(sf.name for sf in dataclasses.fields(section))
    return frozenset(keys | OWN_KEYS)


def unknown_config_keys(flat_config: dict) -> list[str]:
    """Keys NegPy would drop (after applying its legacy-key migrations), sorted."""
    migrated = migrate_flat_config(dict(flat_config))
    return sorted(set(migrated) - known_config_keys())


# --- Engine handle -------------------------------------------------------------
@cache
def _processor() -> ImageProcessor:
    """One CPU ImageProcessor per process (render workers are spawned, so per worker). Its
    single-slot source cache keeps the last decode, so prepare/preview/crop renders of one
    ARW decode it once."""
    return ImageProcessor(use_gpu=False)


# --- Crosstalk profile resolution ------------------------------------------------
def resolve_crosstalk_profile(name: str) -> list[float] | None:
    """Flat 9-float matrix for a crosstalk profile, by display name OR file stem.

    The display name goes through NegPy's own ``CrosstalkProfiles.get_matrix`` (bundled ∪
    the user folder ``APP_CONFIG.crosstalk_dir``, bundled wins). A bare file stem (e.g.
    ``kodak_gold_200``, what our recipes use: stable across upstream's display-name renames)
    is looked up in the same two folders, in the same precedence, and parsed by NegPy's
    parser. "Default" / "Generic C41" / unknown -> None (the engine's built-in matrix).
    """
    if not name or name in ("Default", CrosstalkProfiles.DEFAULT_NAME):
        return None
    matrix = CrosstalkProfiles.get_matrix(name)
    if matrix is not None:
        return list(matrix)
    for directory in (get_resource_path("crosstalk"), APP_CONFIG.crosstalk_dir):
        path = os.path.join(directory, f"{name}.toml")
        parsed = CrosstalkProfiles._parse_file(path) if os.path.isfile(path) else None
        if parsed is not None:
            return list(parsed[1])
    return None


def build_workspace_config(flat_config: dict) -> WorkspaceConfig:
    """Flat dict -> WorkspaceConfig via NegPy's own from_flat_dict migration.

    Also bakes a `crosstalk_profile` name into the `crosstalk_matrix` field the render path
    consumes — the desktop app does this on dropdown selection; here we do it eagerly so a
    config can just name a film stock.
    """
    # from_flat_dict mutates (pop): work on a copy, without negmcp's own keys
    data = {k: v for k, v in flat_config.items() if k not in OWN_KEYS}

    profile = data.get("crosstalk_profile")
    if profile and profile not in ("Default", CrosstalkProfiles.DEFAULT_NAME) and not data.get("crosstalk_matrix"):
        matrix = resolve_crosstalk_profile(profile)
        if matrix is not None:
            data["crosstalk_matrix"] = matrix
        else:
            # Silent fallback to the built-in matrix would ruin a whole roll's colour.
            log.warning(
                "crosstalk profile %r not found (bundled %s, user %s) -- rendering with NegPy's built-in default matrix",
                profile,
                get_resource_path("crosstalk"),
                APP_CONFIG.crosstalk_dir,
            )

    return WorkspaceConfig.from_flat_dict(data)


# --- Iteration sizing (negmcp's own: the app exports full-res only) --------------
def long_edge_for_output(shape: tuple[int, int], crop_rect, target: int) -> int:
    """Long edge to downsample the WHOLE (pre-crop) frame to, so that the cropped OUTPUT's
    long edge comes out at ~``target`` px.

    Passing the iteration size straight through would shrink a half-frame crop (~0.82 of the
    scan height) far below it. ``shape`` is the TRANSFORMED frame's (h, w) — the space
    ``crop_rect`` is drawn in. Uses the crop's fractional size only (the pixel
    ``autocrop_offset`` margin is ignored); rounds up so the output is never smaller than asked.
    """
    h, w = shape
    if crop_rect:
        x0, y0, x1, y1 = crop_rect
        crop_long = max((x1 - x0) * w, (y1 - y0) * h)
    else:
        crop_long = max(h, w)
    return math.ceil(target * max(h, w) / crop_long)


def _transformed_shape(shape: tuple[int, ...], cfg: WorkspaceConfig) -> tuple[int, int]:
    """(h, w) after the geometry stage: a quarter turn swaps them; fine rotation, distortion and
    keystone keep the canvas."""
    h, w = shape[:2]
    return (w, h) if cfg.geometry.rotation % 2 else (h, w)


def _source(arw_path: str, cfg: WorkspaceConfig, long_edge: int | None, output_long_edge: int | None):
    """NegPy's decode (+ optional pre-pipeline shrink). Returns (f32 buffer, source colour space,
    full-res transformed (h, w)). Never mutates the processor's cached source buffer."""
    if long_edge and output_long_edge:
        raise ValueError("pass long_edge OR output_long_edge, not both")
    f32, _ir, source_cs = _processor()._load_source_f32(arw_path, cfg)
    full = _transformed_shape(f32.shape, cfg)
    if output_long_edge:
        long_edge = long_edge_for_output(full, cfg.geometry.crop_rect, output_long_edge)
    if long_edge:
        f32 = _downsample_to_long_edge(f32, long_edge)
    return f32, source_cs, full


def _iteration_layout(cfg: WorkspaceConfig, downsampled: bool) -> WorkspaceConfig:
    """A Print / target-px export resolution would re-inflate a shrunk render to print size in
    the layout step (``render_display_array`` bounds it with a virtual DPI for the same reason);
    an iteration render keeps the size it was rendered at."""
    if not downsampled or cfg.export.export_resolution_mode == ExportResolutionMode.ORIGINAL.value:
        return cfg
    return dataclasses.replace(
        cfg, export=dataclasses.replace(cfg.export, export_resolution_mode=ExportResolutionMode.ORIGINAL.value)
    )


def _run_engine(arw_path: str, img: np.ndarray, cfg: WorkspaceConfig) -> tuple[np.ndarray, dict]:
    """``ImageProcessor.run_pipeline`` on the CPU, exactly as ``_render_export_buffer``'s CPU
    branch calls it, then the export layout. Returns (working-space encoded float, metrics)."""
    ip = _processor()
    cam_xyz, camera_wb = ip._cam_xyz_by_path.get(arw_path, (None, None))
    if cfg.export.icc_input_path:
        cam_xyz = wb_only_cam_xyz(cam_xyz)
    buffer, metrics = ip.run_pipeline(
        img,
        cfg,
        f"negmcp|{arw_path}",
        render_size_ref=float(APP_CONFIG.preview_render_size),
        prefer_gpu=False,
        wants_uv_grid=False,
        cache_stages=False,
        skip_flatfield=True,  # _load_source_f32 already flat-fielded + sensor-unmixed it
        cam_xyz=cam_xyz,
        camera_wb=camera_wb,
    )
    buffer = ip._apply_scaling_and_border_f32(buffer, cfg, cfg.export)
    ip.engine_cpu.cache.clear()
    return buffer, metrics


# --- Output ---------------------------------------------------------------------------
def _target_color_space(cfg: WorkspaceConfig, source_cs: str | None) -> str:
    """Export target, resolved as ``_render_export_buffer`` does: "Same as Source" -> the
    decoded source space (the working space for a sensor-native RAW)."""
    target = cfg.export.export_color_space
    if target == ColorSpace.SAME_AS_SOURCE.value:
        target = source_cs or WORKING_COLOR_SPACE
    return str(target)


def _to_pil(buffer: np.ndarray, cfg: WorkspaceConfig, flat_config: dict, source_cs: str | None) -> Image.Image:
    """Engine float buffer -> 8-bit PIL tagged (``info["icc_profile"]``) like the app's export.

    Default: NegPy's export pixels (``_export_pixels``: quantise + ICC working->target, relative
    colorimetric + BPC). ``output_working_space`` (passthrough): NegPy builds before 0.53 wrote
    their exports without the working->target transform, so the working-space buffer goes out
    as-is (``buffer_to_pil``) and is merely TAGGED with the target profile — a deliberate
    mis-tag when the target is sRGB, kept for parity with such exports;
    ``export_color_space: "Same as Source"`` gets the honest Adobe RGB file.
    """
    ip = _processor()
    target = _target_color_space(cfg, source_cs)
    if buffer.ndim == 3 and buffer.shape[2] == 4:
        buffer = buffer[:, :, :3]
    if output_working_space(flat_config):
        out = ip.buffer_to_pil(buffer, cfg)
        icc = ip._get_target_icc_bytes(target, None)
    else:
        pixels, icc = ip._export_pixels(
            buffer,
            8,
            target == ColorSpace.GREYSCALE.value,
            WORKING_COLOR_SPACE,
            target,
            cfg.export.icc_output_path,
            cfg.export.icc_input_path,
        )
        out = Image.fromarray(pixels)
    if icc:
        out.info["icc_profile"] = icc
    return out


# NegPy's own per-frame meters (base stage, context.metrics), attached to every render as
# ``image.info["negpy_metrics"]``. Units: shadow_point / highlight_point / metered_anchor are
# normalised negative luma in [0, 1] against the frame's bounds (0 = floor, 1 = ceil; P99 / P2
# of the textured block-median grid, anchor = trimmed-mean/midpoint blend pulled 20 % from 0.46
# and clamped to 0.46 +/- 0.12); textural_range is the P10-P90 luma spread of the prefiltered
# log image in log10 density. A slide (transfer path) meters against its fixed window instead.
NEGPY_METRICS = ("shadow_point", "highlight_point", "metered_anchor", "textural_range")


def negpy_metrics(metrics: dict) -> dict[str, float]:
    return {k: round(float(metrics[k]), 5) for k in NEGPY_METRICS if metrics.get(k) is not None}


def pixel_color_space(flat_config: dict) -> str:
    """The colour space the rendered pixel VALUES are in (not the ICC tag): the working space
    under working-space passthrough, else the export target."""
    if output_working_space(flat_config):
        return WORKING_COLOR_SPACE
    return _target_color_space(build_workspace_config(flat_config), None)


def to_srgb(img: Image.Image, source_cs: str) -> Image.Image:
    """Re-express pixel values in ``source_cs`` as sRGB through NegPy's export colour
    management — what a colour-managed sRGB render would have written."""
    srgb = ColorSpace.SRGB.value
    if source_cs == srgb:
        return img
    out, _ = _processor().apply_color_management(img.convert("RGB"), source_cs, srgb, None, None)
    return out


def save_jpeg(
    img: Image.Image, path, *, quality: int = 92, exif: bytes | None = None, subsampling: int | None = None
) -> None:
    """Save a render as JPEG, carrying its ICC profile (``img.info["icc_profile"]``) so the
    file is tagged with the colour space its code values are actually in."""
    kw: dict = {"quality": quality}
    if subsampling is not None:
        kw["subsampling"] = subsampling
    if exif:
        kw["exif"] = exif
    if img.info.get("icc_profile"):
        kw["icc_profile"] = img.info["icc_profile"]
    img.save(path, **kw)


# --- Render --------------------------------------------------------------------------
def _render_cfg(
    arw_path: str, cfg: WorkspaceConfig, flat_config: dict, long_edge: int | None, output_long_edge: int | None
) -> Image.Image:
    img, source_cs, _ = _source(arw_path, cfg, long_edge, output_long_edge)
    cfg = _iteration_layout(cfg, bool(long_edge or output_long_edge))
    buffer, metrics = _run_engine(arw_path, img, cfg)
    out = _to_pil(buffer, cfg, flat_config, source_cs)
    out.info["negpy_metrics"] = negpy_metrics(metrics)
    return out


def render(
    arw_path: str, flat_config: dict, long_edge: int | None = None, output_long_edge: int | None = None
) -> Image.Image:
    """Render one ARW to a positive PIL.Image with NegPy's engine.

    `output_long_edge`: the iteration path — the source is shrunk before the pipeline so the
    CROPPED output's long edge is ~that many px (half-frame aware). Used by the batch
    (non-final) and the MCP tools; None (and no `long_edge`) = full resolution, the app's
    export.

    `long_edge`: shrink the whole (pre-crop) source to this long edge instead. The tone/colour
    stages are statistical over the frame, so decisions made at iteration size transfer to the
    full-res final; full-res only on FINAL (docs/workflow.md).

    The image carries ``info["icc_profile"]`` and ``info["negpy_metrics"]``.
    """
    return _render_cfg(arw_path, build_workspace_config(flat_config), flat_config, long_edge, output_long_edge)


def prepare_frame(arw_path: str, flat_config: dict, long_edge: int | None = None) -> dict:
    """Decode (+ shrink) + NegPy's GeometryProcessor, without the tone stages: the
    ``{"img", "context", ...}`` a roll analysis (negmcp.pool) meters on, the same way
    NegPy's NormalizationWorker does (geometry-applied linear buffer, ``context.active_roi``
    = the crop)."""
    cfg = build_workspace_config(flat_config)
    img, _, _ = _source(arw_path, cfg, long_edge, None)
    cam_xyz, camera_wb = _processor()._cam_xyz_by_path.get(arw_path, (None, None))
    context = PipelineContext(
        original_size=(img.shape[0], img.shape[1]),
        scale_factor=max(img.shape[:2]) / float(APP_CONFIG.preview_render_size),
        process_mode=cfg.process.process_mode,
        cam_xyz=cam_xyz,
        camera_wb=camera_wb,
    )
    img = GeometryProcessor(cfg.geometry).process(img, context)
    return {"arw_path": arw_path, "img": img, "context": context, "scale_factor": context.scale_factor}


def analyze_normalization(arw_path: str, flat_config: dict) -> dict:
    """NegPy's base stage on the full-res frame (``engine.base_processor``: the measured
    normalization, or a slide's fixed transfer window); returns ``context.metrics``
    (floors/ceils, metered_anchor, textural_range, ...). Backs the MCP ``analyze_frame`` tool."""
    prepared = prepare_frame(arw_path, flat_config)
    base_processor(build_workspace_config(flat_config)).process(prepared["img"], prepared["context"])
    return prepared["context"].metrics


# --- Rebate-safe, format-aware crops ---------------------------------------------------
PREVIEW_LONG_EDGE = 900  # px; cheap, discarded render used ONLY to locate the rebate boundary


def _apply_crop_inset(rect: list, suffix: str, crop_inset: dict) -> list:
    """Per-frame manual tightening of a `crop.detect_crops` rect, layered on
    TOP of detection -- for the rare scan where the generic detector's offset
    priors miss a particular roll's holder/scanner offset (e.g. a residual
    rebate strip on one edge of a handful of frames) and a global re-tune of
    `crop.py`'s priors would be overkill / risk regressing the other frames
    that already detect cleanly. A per-frame GEOMETRY key (one of OWN_KEYS,
    same contract as `rotation`) -- never a tone/colour knob.

    `crop_inset` is fractions (of the DETECTED rect's OWN width/height, not
    the full scan) to shave inward on each side, either:
      - flat: {"top": .., "right": .., "bottom": .., "left": ..} -- applies
        to every crop this ARW produces (the common case: 35mm has one crop,
        half-frame would apply the same inset to both halves), or
      - suffix-keyed: {"_L": {...}, "_R": {...}} -- targets one half-frame
        side only, for a defect that only affects one half of the ARW.
    Missing sides / missing suffix entry -> 0 (no-op). Applied AFTER
    aspect-ratio flagging is computed by the caller (uses the pre-inset rect
    for that check, since the inset is a deliberate manual tighten, not a
    detection failure to flag)."""
    if any(k in crop_inset for k in ("top", "bottom", "left", "right")):
        ins = crop_inset
    else:
        ins = crop_inset.get(suffix, {})
    if not ins:
        return rect
    x0, y0, x1, y1 = rect
    rw, rh = x1 - x0, y1 - y0
    return [
        x0 + ins.get("left", 0.0) * rw,
        y0 + ins.get("top", 0.0) * rh,
        x1 - ins.get("right", 0.0) * rw,
        y1 - ins.get("bottom", 0.0) * rh,
    ]


def render_crops(
    arw_path: str, flat_config: dict, fmt: str, long_edge: int | None = None, preview_long_edge: int = PREVIEW_LONG_EDGE
) -> list:
    """Format-aware, rebate-safe render of a frame WITHOUT a manual crop.

    A cheap, unsharpened, uncropped preview render locates the rebate (`crop.detect_crops`,
    one rect for 35mm, "_L"/"_R" for half-frame); each detected rect then becomes the
    frame's ``crop_rect`` and is rendered by the engine. The rect is the engine's crop ROI,
    so NegPy's own meters read only the picture area (``resolve_analysis_region``) — the
    rebate never reaches the analysis. `long_edge`: each crop's OUTPUT long edge.

    Returns one dict per crop: {"suffix", "image" (PIL), "rect" (fraction [x0,y0,x1,y1] in
    the transformed frame), "aspect_flag" (bool, True = flag for review), "aspect_ratio",
    "expected_aspect_ratio"}.
    """
    cfg = build_workspace_config(flat_config)
    geo, lab = cfg.geometry, cfg.lab
    preview_cfg = dataclasses.replace(
        cfg, geometry=dataclasses.replace(geo, crop_rect=None), lab=dataclasses.replace(lab, sharpen=0.0)
    )
    preview = _render_cfg(arw_path, preview_cfg, flat_config, preview_long_edge, None).convert("RGB")
    detected = crop.detect_crops(preview, fmt)  # [(rect, suffix, aspect_flag), ...]
    _, _, (h, w) = _source(arw_path, cfg, None, None)

    crop_inset = flat_config.get("crop_inset")
    results = []
    for rect, suffix, is_flagged in detected:
        if crop_inset:
            rect = _apply_crop_inset(rect, suffix, crop_inset)
        crop_cfg = dataclasses.replace(cfg, geometry=dataclasses.replace(geo, crop_rect=tuple(rect)))
        out = _render_cfg(arw_path, crop_cfg, flat_config, None, long_edge)
        _, actual_ratio, expected_ratio = crop.aspect_flag(rect, fmt, w, h, suffix)
        results.append(
            {
                "suffix": suffix,
                "image": out,
                "rect": rect,
                "aspect_flag": is_flagged,
                "aspect_ratio": actual_ratio,
                "expected_aspect_ratio": expected_ratio,
            }
        )
    return results
