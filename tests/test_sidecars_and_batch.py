import json

import pytest
from conftest import BASE

from negmcp.batch import build_tasks, resolve_format
from negmcp.crop import DEFAULT_CROP
from negmcp.exif import RollFormat
from negmcp.recipe import SidecarRefusedError, write_sidecars


@pytest.fixture
def roll(tmp_path):
    def _roll(fmt_line: str, stems: list[str]):
        d = tmp_path / "neg" / "r1"
        d.mkdir(parents=True)
        (d / "info.txt").write_text(f"film: test\nformat: {fmt_line}\n")
        for s in stems:
            (d / s).write_bytes(b"")
        return d

    return _roll


def test_write_sidecars_full_frame_from_recipe(roll, make_recipe):
    d = roll("35mm", ["F1.ARW", "F2.arw", "F3.ARW"])
    recipe = make_recipe(
        {"base": BASE, "per_frame_overrides": {"F2": {"grade": 120}, "F3": "skip: blank"}}, name="r1_recipe.json"
    )
    written, skipped = write_sidecars(recipe)
    assert [p.name for p in written] == ["F1.negpy", "F2.negpy"]
    assert skipped == ["F3(recipe: skip: blank)"]
    side = json.loads((d / "F2.negpy").read_text())
    assert "output_working_space" not in json.dumps(side)


def test_write_sidecars_refuses_half_frame(roll, make_recipe):
    d = roll("half-frame  (each ARW = 2 halves)", ["A.ARW"])
    recipe = make_recipe({"base": BASE, "per_frame_overrides": {}}, name="r1_recipe.json")
    with pytest.raises(SidecarRefusedError, match="half-frame"):
        write_sidecars(recipe)
    assert not list(d.glob("*.negpy"))


def test_half_frame_tasks_case_insensitive_and_skips(roll, tmp_path):
    d = roll("half-frame", ["A.ARW", "B.arw", "C.ARW"])
    recipe = {
        "base": BASE,
        "per_frame_overrides": {"A": {"R": "skip: blank", "L": {"wb_yellow": 0.1}}, "C": "skip: end of film"},
    }
    assert resolve_format(d, None) is RollFormat.HALF
    tasks, skipped = build_tasks(d, recipe, RollFormat.HALF, tmp_path / "out", 2200)
    assert [t.out.name for t in tasks] == ["A_L.jpg", "B_L.jpg", "B_R.jpg"]
    assert tasks[0].config["wb_yellow"] == 0.1 and tasks[0].config["manual_crop_rect"] == DEFAULT_CROP["L"]
    assert tasks[2].config["manual_crop_rect"] == DEFAULT_CROP["R"]
    assert [n for n, _ in skipped] == ["A_R.jpg", "C_L.jpg", "C_R.jpg"]


def test_format_must_be_known(roll):
    d = roll("", [])
    with pytest.raises(ValueError, match="--format"):
        resolve_format(d, None)
    assert resolve_format(d, "ff") is RollFormat.FF
