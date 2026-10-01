import json
import multiprocessing as mp
import os

import pytest
from conftest import BASE

from negmcp.config import get_config
from negmcp.crop import DEFAULT_CROP
from negmcp.exif import RollFormat
from negmcp.recipe import RecipeError, apply_fix, load_recipe, validate_recipe


def test_valid_recipe_passes_with_notes_skips_and_legacy_keys():
    validate_recipe(
        {
            "_note": "free text",
            "base": BASE,
            "per_frame_overrides": {
                "A": {"L": {"manual_crop_rect": [0, 0, 0.5, 1], "wb_yellow": 0.02}, "R": "skip: blank"},
                "B": "skip: black frame",
            },
        },
        fmt=RollFormat.HALF,
    )
    validate_recipe({"base": BASE, "per_frame_overrides": {"A": {"grade": 120}}}, fmt=RollFormat.FF)


@pytest.mark.parametrize(
    ("recipe", "fmt", "message"),
    [
        (
            {"base": {k: v for k, v in BASE.items() if k != "output_working_space"}},
            None,
            "output_working_space missing",
        ),
        ({"base": {**BASE, "output_working_space": 1}}, None, "must be true/false"),
        ({"base": BASE, "notes": "x"}, None, "unknown top-level key(s) ['notes']"),
        ({"base": {**BASE, "wb_yelow": 0.1}}, None, "base: unknown key(s) ['wb_yelow']"),
        ({"base": BASE, "per_frame": {"F1": {"crop": [0, 0, 1, 1], "angle": 0.1}}}, None, "['per_frame']"),
        (
            {"base": BASE, "per_frame_overrides": {"F1": {"crop": [0, 0, 1, 1], "angle": 0.1}}},
            RollFormat.FF,
            "per_frame_overrides.F1: unknown key(s) ['angle', 'crop']",
        ),
        ({"base": BASE, "per_frame_overrides": {"A": {"L": {"grade": 1}}}}, RollFormat.FF, "L/R entry in a full-frame"),
        ({"base": BASE, "per_frame_overrides": {"A": {"grade": 1}}}, RollFormat.HALF, "half-frame roll needs"),
        ({"per_frame_overrides": {}}, None, "missing 'base'"),
    ],
)
def test_invalid_recipes_are_rejected(recipe, fmt, message):
    with pytest.raises(RecipeError) as exc:
        validate_recipe(recipe, fmt=fmt)
    assert message in str(exc.value)


def test_apply_fix_merges_and_keeps_prior_fixes(make_recipe):
    path = make_recipe({"_note": "keep me", "base": BASE, "per_frame_overrides": {}})
    apply_fix(path, "DSC1", "L", {"wb_yellow": 0.05})
    merged = apply_fix(path, "DSC1", "L", {"black_point_offset": -0.1})
    assert merged == {"wb_yellow": 0.05, "black_point_offset": -0.1, "manual_crop_rect": DEFAULT_CROP["L"]}
    r = load_recipe(path)
    assert r["_note"] == "keep me"
    assert r["per_frame_overrides"]["DSC1"]["L"] == merged


def test_apply_fix_full_frame(make_recipe):
    path = make_recipe({"base": BASE, "per_frame_overrides": {"F1": {"grade": 120}}})
    assert apply_fix(path, "F1", "FF", {"density": 1.1}) == {"grade": 120, "density": 1.1}


def test_apply_fix_refuses_invalid_result_and_leaves_file_untouched(make_recipe):
    path = make_recipe({"base": BASE, "per_frame_overrides": {}})
    before = path.read_bytes()
    with pytest.raises(RecipeError, match="wb_yelow"):
        apply_fix(path, "DSC1", "R", {"wb_yelow": 0.1})
    assert path.read_bytes() == before


def test_apply_fix_preserves_file_mode(make_recipe):
    path = make_recipe({"base": BASE, "per_frame_overrides": {}})
    os.chmod(path, 0o640)
    apply_fix(path, "DSC1", "R", {"grade": 110})
    assert path.stat().st_mode & 0o777 == 0o640
    assert not list(path.parent.glob(".*.tmp"))


def _fixer(args):
    path, prefix, n, env = args
    os.environ.update(env)
    get_config.cache_clear()
    for i in range(n):
        apply_fix(path, f"{prefix}{i:03d}", "FF", {"grade": 100 + i})


def test_concurrent_fixers_never_lose_frames(make_recipe):
    path = make_recipe({"base": BASE, "per_frame_overrides": {}})
    env = {k: v for k, v in os.environ.items() if k.startswith("NEGMCP_")}
    n = 15
    with mp.get_context("spawn").Pool(2) as pool:
        pool.map(_fixer, [(path, "A", n, env), (path, "B", n, env)])
    pf = json.loads(path.read_text())["per_frame_overrides"]
    assert sorted(pf) == sorted([f"A{i:03d}" for i in range(n)] + [f"B{i:03d}" for i in range(n)])
    assert pf["B007"] == {"grade": 107}
