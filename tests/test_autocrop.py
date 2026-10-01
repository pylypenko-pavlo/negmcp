"""negmcp autocrop: NegPy's batch_autocrop is monkeypatched; the glue (frame selection, half-frame
mapping, write-back through the recipe) runs for real."""

import json

import numpy as np
import pytest
from conftest import BASE

from negmcp import autocrop, cli
from negmcp.exif import RollFormat
from negmcp.recipe import RecipeError, load_recipe, validate_recipe


def _roll(tmp_path, stems, fmt="35mm"):
    roll = tmp_path / "neg" / "r1"
    roll.mkdir(parents=True)
    (roll / "info.txt").write_text(f"film: x\nformat: {fmt}\nnegpy_crosstalk_profile: kodak_gold_200\n")
    for s in stems:
        (roll / f"{s}.ARW").write_bytes(b"")
    return roll


@pytest.fixture
def fake_negpy(monkeypatch):
    """Decode -> blank preview; detection records the keys; resolution answers from ``answers``."""
    seen: list[str] = []
    answers: dict = {}

    monkeypatch.setattr(autocrop, "_decode", lambda pm, arw, cfg: np.zeros((40, 60, 3), np.float32))

    def detect(key, image, *, target_ratio, rebate_trim):
        seen.append(key)
        return autocrop.CropEvidence(key, image.shape[:2], None, 0.0, 0.0, reason="no_consensus")

    monkeypatch.setattr(autocrop, "detect_crop_candidate", detect)
    monkeypatch.setattr(autocrop, "resolve_roll_crops", lambda ev: [answers[e.key] for e in ev if e.key in answers])
    monkeypatch.setattr(autocrop, "detect_split_and_crop_for_file", lambda path: (0.5, 0.1, (0.1, 0.0, 0.9, 1.0)))
    return seen, answers


def _crop(key, rect, angle=0.0):
    from negpy.features.geometry.batch_autocrop import ResolvedCrop  # after negmcp bootstrapped the pin

    return ResolvedCrop(key=key, crop_rect=rect, correction_angle=angle, confidence=0.8, calibrated=True)


def test_ff_frames_preserved_skipped_and_resolved(tmp_path, fake_negpy):
    seen, answers = fake_negpy
    roll = _roll(tmp_path, ["A", "B", "C", "D"])
    answers["A"] = _crop("A", (0.05, 0.04, 0.95, 0.93), angle=-0.4)
    recipe = {
        "base": {**BASE, "manual_crop_rect": [0, 0, 1, 1]},  # a base crop is a roll default, not a frame's own
        "per_frame_overrides": {
            "B": {"manual_crop_rect": [0.1, 0.1, 0.9, 0.9]},
            "C": "skip: blank",
            "D": {"fine_rotation": 0.3},
        },
    }
    res = autocrop.autocrop_roll(roll, recipe, RollFormat.FF)
    assert sorted(seen) == ["A", "D"]
    assert res.preserved == ["B"] and res.skipped == ["C"]
    assert res.unresolved == {"D": "no_consensus"}
    (f,) = res.frames
    assert (f.stem, f.side, f.rect, f.fine_rotation) == ("A", "FF", (0.05, 0.04, 0.95, 0.93), -0.4)
    assert autocrop.recipe_fixes(res) == [
        ("A", "FF", {"manual_crop_rect": [0.05, 0.04, 0.95, 0.93], "fine_rotation": -0.4})
    ]

    seen.clear()
    assert autocrop.autocrop_roll(roll, recipe, RollFormat.FF, force=True).preserved == []
    assert sorted(seen) == ["A", "B", "D"]


def test_fine_rotation_adds_to_the_frame_own(tmp_path, fake_negpy):
    _, answers = fake_negpy
    roll = _roll(tmp_path, ["D"])
    answers["D"] = _crop("D", (0.1, 0.1, 0.9, 0.9), angle=0.25)
    res = autocrop.autocrop_roll(
        roll, {"base": BASE, "per_frame_overrides": {"D": {"fine_rotation": 0.3}}}, RollFormat.FF
    )
    assert res.frames[0].fine_rotation == 0.55


def test_half_frame_rect_maps_to_full_scan_like_slice_half(tmp_path, fake_negpy):
    seen, answers = fake_negpy
    roll = _roll(tmp_path, ["H"], fmt="half-frame")
    answers["H_L"] = _crop("H_L", (0.0, 0.0, 1.0, 1.0))
    answers["H_R"] = _crop("H_R", (0.0, 0.0, 1.0, 0.5), angle=0.7)  # a deskew on a half has no full-scan form
    res = autocrop.autocrop_roll(roll, {"base": BASE, "per_frame_overrides": {}}, RollFormat.HALF)
    assert sorted(seen) == ["H_L", "H_R"]
    (left,) = res.frames
    # split 0.5 of the film width 0.8, gutter 0.1 of it -> L ends at 0.1 + 0.8 * 0.45
    assert (left.name, left.rect) == ("H_L", (0.1, 0.0, 0.46, 1.0))
    assert "deskew" in res.unresolved["H_R"]
    assert res.splits["H"]["split_x"] == 0.5


def test_half_with_its_own_rotation_is_refused(tmp_path, fake_negpy):
    seen, _ = fake_negpy
    roll = _roll(tmp_path, ["H"], fmt="half-frame")
    recipe = {"base": BASE, "per_frame_overrides": {"H": {"L": {"rotation": 1}, "R": {}}}}
    res = autocrop.autocrop_roll(roll, recipe, RollFormat.HALF)
    assert seen == ["H_R"] and "rotation" in res.unresolved["H_L"]


def test_cli_apply_writes_crops_through_the_recipe(tmp_path, fake_negpy, capsys):
    _, answers = fake_negpy
    _roll(tmp_path, ["A", "B"])
    answers["A"] = _crop("A", (0.05, 0.04, 0.95, 0.93), angle=-0.4)
    recipe_path = tmp_path / "r1_recipe.json"
    recipe_path.write_text(json.dumps({"base": BASE, "per_frame_overrides": {"A": {"grade": 120}}}))

    assert cli.main(["autocrop", "r1", "--recipe", str(recipe_path)]) == 0
    assert load_recipe(recipe_path)["per_frame_overrides"] == {"A": {"grade": 120}}  # dry run writes nothing

    assert cli.main(["autocrop", "r1", "--recipe", str(recipe_path), "--apply"]) == 0
    pf = load_recipe(recipe_path)["per_frame_overrides"]
    assert pf == {"A": {"grade": 120, "manual_crop_rect": [0.05, 0.04, 0.95, 0.93], "fine_rotation": -0.4}}
    out = capsys.readouterr()
    assert "RESOLVED 1/2" in out.out and "UNRESOLVED 1" in out.err and "'B': 'no_consensus'" in out.err


# --- recipe validation of the crop / autocrop fields ----------------------------------
def test_autocrop_fields_are_accepted():
    validate_recipe(
        {
            "base": {**BASE, "crop_from_auto": True, "autocrop_mode": "image", "autocrop_ratio": "3:2"},
            "per_frame_overrides": {"F1": {"autocrop_rebate_trim": 1.2, "autocrop_offset": 4, "fine_rotation": -0.4}},
        },
        fmt=RollFormat.FF,
    )


@pytest.mark.parametrize(
    ("recipe", "fmt", "message"),
    [
        ({"base": {**BASE, "autocrop_mode": "imgae"}}, None, "base.autocrop_mode"),
        ({"base": {**BASE, "autocrop_ratio": "3x2"}}, None, "base.autocrop_ratio"),
        ({"base": {**BASE, "autocrop_rebate_trim": -1}}, None, "autocrop_rebate_trim"),
        ({"base": {**BASE, "crop_from_auto": "yes"}}, None, "crop_from_auto: expected true/false"),
        ({"base": BASE, "per_frame_overrides": {"F": {"fine_rotation": 99}}}, None, "F.fine_rotation"),
        (
            {"base": BASE, "per_frame_overrides": {"F": {"manual_crop_rect": [0.5, 0, 0.4, 1]}}},
            None,
            "manual_crop_rect",
        ),
        ({"base": {**BASE, "crop_from_auto": True}}, RollFormat.HALF, "half-frame roll"),
        (
            {
                "base": {**BASE, "crop_from_auto": True},
                "per_frame_overrides": {"F": {"manual_crop_rect": [0, 0, 1, 1]}},
            },
            RollFormat.FF,
            "drops the rect",
        ),
    ],
)
def test_bad_crop_fields_are_rejected(recipe, fmt, message):
    with pytest.raises(RecipeError) as err:
        validate_recipe(recipe, fmt=fmt)
    assert message in str(err.value)
