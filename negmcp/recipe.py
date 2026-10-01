"""Roll recipe = the single source of truth for a roll (``<rolls_dir>/<roll>_recipe.json``).

Shape::

    {"base": {flat NegPy config, incl. "output_working_space": true|false},
     "per_frame_overrides": {
         "<stem>": {"L": {...} | "<skip reason>", "R": ...}   # half-frame
         "<stem>": {...} | "<skip reason>"                    # full-frame
     },
     "_graded_negpy_version": "0.62.0",                       # stamped by `batch --final`
     "_anything": ...}                                       # notes, ignored

Every fix (colourist agent, fixer, manual) goes through :func:`apply_fix` — never hand-patch review
JPGs; the review folder is always regenerated from the recipe.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from negmcp.config import get_config
from negmcp.crop import DEFAULT_CROP
from negmcp.exif import RollFormat

__all__ = [
    "GRADED_VERSION_KEY",
    "RecipeError",
    "SidecarRefusedError",
    "apply_fix",
    "apply_fixes",
    "load_recipe",
    "engine_drift",
    "new_recipe",
    "stamp_graded_version",
    "validate_recipe",
    "write_sidecar_file",
    "write_sidecars",
]

SIDES = ("L", "R")
FULL_FRAME = "FF"
TOP_LEVEL_KEYS = frozenset({"base", "per_frame_overrides"})
# NegPy version the roll's finals were rendered on; written only by a full `batch --final`.
GRADED_VERSION_KEY = "_graded_negpy_version"


class RecipeError(ValueError):
    """The recipe breaks the schema; ``problems`` lists every violation found."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(f"invalid recipe {source}:\n  - " + "\n  - ".join(problems))


CROP_RECT_KEYS = ("manual_crop_rect", "crop_rect")  # legacy name (our recipes) and NegPy's own


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _check_geometry(where: str, flat: Mapping, problems: list[str]) -> None:
    """Values of the crop / autocrop fields. NegPy's GeometryConfig silently coerces a bad
    ``autocrop_mode`` / ``autocrop_ratio`` to its default, so a typo would crop the whole roll
    differently without a word."""
    from negpy.features.geometry.models import FINE_ROTATION_LIMIT, AspectRatio, AutocropMode

    for key in CROP_RECT_KEYS:
        rect = flat.get(key)
        if rect is None:
            continue
        ok = isinstance(rect, list | tuple) and len(rect) == 4 and all(_is_number(v) for v in rect)
        if not ok or not (0.0 <= rect[0] < rect[2] <= 1.0 and 0.0 <= rect[1] < rect[3] <= 1.0):
            problems.append(f"{where}.{key}: expected [x0, y0, x1, y1] fractions with 0 <= x0 < x1 <= 1, got {rect!r}")
    checks = {
        "crop_from_auto": (lambda v: isinstance(v, bool), "true/false"),
        "autocrop_mode": (lambda v: v in {m.value for m in AutocropMode}, sorted(m.value for m in AutocropMode)),
        "autocrop_ratio": (lambda v: v in {r.value for r in AspectRatio}, "one of NegPy's AspectRatio values"),
        "autocrop_rebate_trim": (lambda v: _is_number(v) and v >= 0, "a number >= 0"),
        "autocrop_offset": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer (px)"),
        "fine_rotation": (
            lambda v: _is_number(v) and abs(v) <= FINE_ROTATION_LIMIT,
            f"degrees within +/-{FINE_ROTATION_LIMIT}",
        ),
    }
    for key, (ok, expected) in checks.items():
        if key in flat and not ok(flat[key]):
            problems.append(f"{where}.{key}: expected {expected}, got {flat[key]!r}")


def _check_flat(where: str, flat: object, problems: list[str]) -> None:
    from negmcp.render import unknown_config_keys

    if not isinstance(flat, Mapping):
        problems.append(f"{where}: expected an object of flat NegPy keys, got {type(flat).__name__}")
        return
    unknown = unknown_config_keys(dict(flat))
    if unknown:
        problems.append(f"{where}: unknown key(s) {unknown}")
    if "output_working_space" in flat and not isinstance(flat["output_working_space"], bool):
        problems.append(f"{where}.output_working_space: must be true/false, got {flat['output_working_space']!r}")
    _check_geometry(where, flat, problems)


def _check_armed_autocrop(where: str, merged: Mapping, half: bool, problems: list[str]) -> None:
    """``crop_from_auto: true`` arms NegPy's per-render autocrop. The engine then detects on the
    whole scan and overwrites any rect whose ``crop_detect_key`` is stale — i.e. every rect a
    recipe holds (half-frame L/R crops included)."""
    if merged.get("crop_from_auto") is not True:
        return
    if half:
        problems.append(
            f"{where}: crop_from_auto on a half-frame roll would replace the L/R crop with a whole-scan detection"
        )
    elif any(merged.get(k) for k in CROP_RECT_KEYS) and not merged.get("crop_detect_key"):
        problems.append(f"{where}: crop_from_auto together with a crop rect: the engine re-detects and drops the rect")


def _is_half_entry(entry: Mapping) -> bool:
    return bool(entry) and set(entry) <= set(SIDES)


def validate_recipe(recipe: object, *, fmt: RollFormat | None = None, source: str = "<recipe>") -> None:
    """Raise RecipeError unless ``recipe`` follows the schema in the module docstring.

    Checks: only ``base`` / ``per_frame_overrides`` / ``_``-prefixed notes at the top level;
    ``base.output_working_space`` present (bool); every base / override key known to NegPy
    (after its legacy migrations) or to negmcp; frame entries shaped half (L/R) or flat —
    and matching ``fmt`` when given.
    """
    problems: list[str] = []
    if not isinstance(recipe, Mapping):
        raise RecipeError(source, [f"top level must be an object, got {type(recipe).__name__}"])
    extra = sorted(k for k in recipe if k not in TOP_LEVEL_KEYS and not k.startswith("_"))
    if extra:
        problems.append(f"unknown top-level key(s) {extra} (allowed: base, per_frame_overrides, _notes)")
    base = recipe.get("base")
    if not isinstance(base, Mapping):
        problems.append("missing 'base' object")
    else:
        _check_flat("base", base, problems)
        if "output_working_space" not in base:
            problems.append(
                "base.output_working_space missing: the colour mode must be explicit "
                "(true = working-space passthrough, false = colour-managed export)"
            )
    pf = recipe.get("per_frame_overrides", {})
    if not isinstance(pf, Mapping):
        problems.append("per_frame_overrides must be an object {stem: override}")
        pf = {}
    base_map = base if isinstance(base, Mapping) else {}
    half_roll = fmt is RollFormat.HALF or any(isinstance(e, Mapping) and _is_half_entry(e) for e in pf.values())
    _check_armed_autocrop("base", base_map, half_roll, problems)
    for stem, entry in pf.items():
        if isinstance(entry, Mapping):
            sides = entry.items() if _is_half_entry(entry) else [("", entry)]
            for side, side_entry in sides:
                if isinstance(side_entry, Mapping) and {"crop_from_auto", *CROP_RECT_KEYS} & set(side_entry):
                    where = f"per_frame_overrides.{stem}" + (f".{side}" if side else "")
                    _check_armed_autocrop(where, {**base_map, **side_entry}, half_roll, problems)
        if isinstance(entry, str):
            continue  # recipe skip
        if not isinstance(entry, Mapping):
            problems.append(f"per_frame_overrides.{stem}: expected object or skip string")
        elif _is_half_entry(entry):
            if fmt is RollFormat.FF:
                problems.append(f"per_frame_overrides.{stem}: L/R entry in a full-frame roll")
            for side, side_entry in entry.items():
                if not isinstance(side_entry, str):
                    _check_flat(f"per_frame_overrides.{stem}.{side}", side_entry, problems)
        else:
            if fmt is RollFormat.HALF and entry:
                problems.append(f"per_frame_overrides.{stem}: half-frame roll needs {{'L': ..., 'R': ...}}")
            _check_flat(f"per_frame_overrides.{stem}", entry, problems)
    graded = recipe.get(GRADED_VERSION_KEY)
    if graded is not None and not (isinstance(graded, str) and graded):
        problems.append(f"{GRADED_VERSION_KEY} must be a version string, got {graded!r}")
    if problems:
        raise RecipeError(source, problems)


def engine_drift(recipe: Mapping, current: str) -> str | None:
    """A one-line warning when the recipe was graded on a different NegPy than ``current``."""
    graded = recipe.get(GRADED_VERSION_KEY)
    if graded is None:
        return f"recipe has no {GRADED_VERSION_KEY} (never finalised by `negmcp batch --final`)"
    if graded != current:
        return f"recipe graded on NegPy {graded}, rendering on {current}: check the roll against the refs corridor"
    return None


def load_recipe(path: Path, *, validate: bool = True, fmt: RollFormat | None = None) -> dict:
    with path.open(encoding="utf-8") as fh:
        recipe = json.load(fh)
    if validate:
        validate_recipe(recipe, fmt=fmt, source=str(path))
    return recipe


def _merge_fix(recipe: dict, stem: str, side: str, overrides: Mapping) -> dict:
    """MERGE ``overrides`` into the frame's existing override (prior fixes survive)."""
    pf = recipe.setdefault("per_frame_overrides", {})
    if side == FULL_FRAME:
        existing = pf.get(stem)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(overrides)
        pf[stem] = merged
    elif side in SIDES:
        frame = pf.setdefault(stem, {})
        existing = frame.get(side)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(overrides)
        merged.setdefault("manual_crop_rect", DEFAULT_CROP[side])
        frame[side] = merged
    else:
        raise ValueError(f"side must be L, R, or FF (got {side!r})")
    return merged


@contextmanager
def _recipe_lock(path: Path) -> Iterator[None]:
    """Exclusive inter-process lock for one recipe. The lock file lives in cache_dir (not
    next to the recipe: that folder may be cloud-synced) and is keyed by the resolved path;
    locking the recipe itself would not work because os.replace swaps its inode."""
    lock_dir = get_config().cache_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    with (lock_dir / f"{path.name}.{key}.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: object) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    tmp_path = Path(tmp.name)
    try:
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def apply_fix(path: Path, stem: str, side: str, overrides: Mapping) -> dict:
    """Write a per-frame override INTO the recipe; returns the merged override.

    Read-merge-validate-write runs under an exclusive lock and the write is atomic
    (temp file + os.replace), so concurrent fixers never lose each other's frames and a
    crash never leaves a half-written recipe. An invalid result is not written.
    """
    return apply_fixes(path, [(stem, side, overrides)])[0]


def apply_fixes(path: Path, fixes: list[tuple[str, str, Mapping]]) -> list[dict]:
    """:func:`apply_fix` for many frames in ONE locked read-merge-validate-write (all or none)."""
    if not fixes:
        return []
    sides = {side == FULL_FRAME for _, side, _ in fixes}
    if len(sides) != 1:
        raise ValueError("one call mixes FF and L/R fixes")
    with _recipe_lock(path):
        recipe = load_recipe(path, validate=False)
        merged = [_merge_fix(recipe, stem, side, overrides) for stem, side, overrides in fixes]
        validate_recipe(recipe, fmt=RollFormat.FF if sides == {True} else RollFormat.HALF, source=str(path))
        _atomic_write_json(path, recipe)
    return merged


def stamp_graded_version(path: Path, version: str) -> None:
    """Record the NegPy version the roll's finals were rendered on (same lock + atomic write
    as :func:`apply_fix`; nothing else in the recipe changes)."""
    with _recipe_lock(path):
        recipe = load_recipe(path, validate=False)
        if recipe.get(GRADED_VERSION_KEY) == version:
            return
        recipe[GRADED_VERSION_KEY] = version
        validate_recipe(recipe, source=str(path))
        _atomic_write_json(path, recipe)


def _half_frame_note(roll_dir: Path) -> dict:
    """NegPy's auto-split (gutter + film extent) on every ARW of a half-frame roll, as a recipe
    note. Nothing is applied: the halves keep DEFAULT_CROP (see docs/negpy-compat.md, crops)."""
    from negmcp import _negpy
    from negmcp.crop import detect_half_frame

    frames, failed = {}, {}
    for arw in sorted(p for p in roll_dir.iterdir() if p.suffix.lower() == ".arw"):
        rec = detect_half_frame(arw)
        frames[arw.stem] = rec
        if rec["status"] != "ok":
            failed[arw.stem] = rec["status"]
    return {"negpy": _negpy.installed_version(), "failed": failed, "frames": frames}


def new_recipe(roll_dir: Path, rolls_dir: Path, look_dir: Path) -> tuple[Path, dict]:
    """Birth ``<rolls_dir>/<roll>_recipe.json`` from ``<look_dir>/base_template.json`` + the roll's info.txt.

    Copies the film-agnostic universal base and substitutes ONLY ``crosstalk_profile``
    (info.txt ``negpy_crosstalk_profile:``), then applies the validated per-stock deltas
    from film_deltas.json (+ push deltas when info.txt has ``push:``). Never invents
    per-roll tuning; refuses to overwrite an existing recipe.

    Half-frame rolls also get NegPy's auto-split per ARW recorded as the
    ``_half_frame_detect`` note. Crops are not this function's business: ``negmcp new
    --autocrop`` / ``negmcp autocrop --apply`` write NegPy's roll autocrop (negmcp.autocrop).
    """
    from negmcp.crop import HALF_DETECT_NOTE
    from negmcp.exif import parse_info_txt, roll_format

    roll_name = roll_dir.name
    info_path = roll_dir / "info.txt"
    if not info_path.exists():
        raise FileNotFoundError(f"no info.txt in {roll_dir}")
    info = parse_info_txt(info_path)
    slide = info.get("process_mode", "").lower().startswith("transparency")
    profile = None if slide else info.get("negpy_crosstalk_profile")
    pushed = bool(info.get("push")) and not info.get("push", "").lower().startswith("none")
    if not slide and not profile:
        raise ValueError(f"no negpy_crosstalk_profile: line in {info_path}")

    recipe_path = rolls_dir / f"{roll_name}_recipe.json"
    if recipe_path.exists():
        raise FileExistsError(f"{recipe_path} already exists — not overwriting")

    tpl = load_recipe(look_dir / "base_template.json")
    recipe: dict = {"base": dict(tpl["base"]), "per_frame_overrides": {}}
    recipe["base"]["crosstalk_profile"] = profile

    deltas_path = look_dir / "film_deltas.json"
    fd = json.loads(deltas_path.read_text(encoding="utf-8")) if deltas_path.exists() else {}
    if slide:
        # E-6: no C-41 crosstalk profile can engage; the slide set lives under _transparency.
        recipe["base"]["process_mode"] = "Transparency"
        profile = "_transparency"
    known = profile in fd
    applied = dict(fd.get(profile, {}) if known else {})
    recipe["base"].update(applied)
    if pushed:
        push = fd.get("_push", {}).get(profile) or fd.get("_push", {}).get("_default", {})
        recipe["base"].update(push)
        applied.update(push)
    recipe["_stock_deltas_applied"] = {"profile": profile, "known_stock": known, "pushed": pushed, "deltas": applied}
    if roll_format(info) is RollFormat.HALF:
        recipe[HALF_DETECT_NOTE] = _half_frame_note(roll_dir)

    validate_recipe(recipe, source=str(recipe_path))
    rolls_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(recipe_path, recipe)
    return recipe_path, recipe


class SidecarRefusedError(ValueError):
    """Sidecars cannot faithfully represent this roll (half-frame)."""


def write_sidecar_file(roll_dir: Path, stem: str, flat_cfg: Mapping) -> Path:
    """Atomically write ``<roll_dir>/<stem>.negpy`` via NegPy's own WorkspaceConfig.to_dict()."""
    from negmcp.render import build_workspace_config

    payload = json.dumps(build_workspace_config(dict(flat_cfg)).to_dict(), default=str, indent=2)
    sidecar_path = roll_dir / f"{stem}.negpy"
    with tempfile.NamedTemporaryFile("w", dir=roll_dir, delete=False, suffix=".part", encoding="utf-8") as tmp:
        tmp.write(payload)
    Path(tmp.name).replace(sidecar_path)
    return sidecar_path


def write_sidecars(
    recipe_path: Path, roll_dir: Path | None = None, fmt: RollFormat | None = None
) -> tuple[list[Path], list[str]]:
    """One ``.negpy`` per ARW of a FULL-FRAME roll, straight from its recipe
    (base + that frame's override). Returns (written, skipped-by-recipe).

    Half-frame rolls are refused: NegPy keeps one sidecar per ARW (one crop), so the two
    L/R halves the batch renders cannot both be expressed — writing one would silently
    disagree with the review folder.
    """
    from negmcp.exif import parse_info_txt, roll_format

    if roll_dir is None:
        if not recipe_path.stem.endswith("_recipe"):
            raise ValueError(f"cannot infer the roll from {recipe_path.name}; pass the roll directory")
        roll_dir = get_config().neg_root / recipe_path.stem.removesuffix("_recipe")
    fmt = fmt or roll_format(parse_info_txt(roll_dir / "info.txt"))
    if fmt is None:
        raise SidecarRefusedError(f"{roll_dir}/info.txt has no `format:` line; pass the format (ff) explicitly")
    if fmt is RollFormat.HALF:
        raise SidecarRefusedError(
            f"{roll_dir.name} is half-frame: a NegPy sidecar is one per ARW and holds one crop, "
            "so the L/R halves cannot be written faithfully. Refusing (no sidecars written)."
        )
    recipe = load_recipe(recipe_path, fmt=fmt)
    base, pf = recipe["base"], recipe.get("per_frame_overrides", {})
    written: list[Path] = []
    skipped: list[str] = []
    for arw in sorted(p for p in roll_dir.iterdir() if p.suffix.lower() == ".arw"):
        ov = pf.get(arw.stem, {})
        if isinstance(ov, str):
            skipped.append(f"{arw.stem}(recipe: {ov})")
            continue
        written.append(write_sidecar_file(roll_dir, arw.stem, {**base, **ov}))
    return written, skipped
