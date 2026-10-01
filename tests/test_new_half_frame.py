import json

from conftest import BASE

from negmcp import crop
from negmcp.recipe import load_recipe, new_recipe


def _setup(tmp_path):
    roll = tmp_path / "neg" / "h1"
    roll.mkdir(parents=True)
    (roll / "info.txt").write_text("film: x\nformat: half-frame\nnegpy_crosstalk_profile: kodak_gold_200\n")
    for s in ("A", "B", "C"):
        (roll / f"{s}.ARW").write_bytes(b"")
    adv = tmp_path / "look"
    adv.mkdir()
    (adv / "base_template.json").write_text(json.dumps({"base": BASE}))
    return roll, adv


DETECTED = {
    "A": {"split_x": 0.5, "gutter": 0.05, "film_crop": [0.06, 0.03, 0.93, 0.9], "status": "ok"},
    "B": {"split_x": 0.5, "gutter": 0.0, "film_crop": [0.06, 0.03, 0.93, 0.9], "status": "failed"},
    "C": {"split_x": 0.52, "gutter": 0.05, "film_crop": [0.0, 0.03, 0.93, 0.9], "status": "partial"},
}


def test_new_records_split_detection_without_cropping(tmp_path, monkeypatch):
    roll, adv = _setup(tmp_path)
    monkeypatch.setattr(crop, "detect_half_frame", lambda arw: DETECTED[arw.stem])
    path, recipe = new_recipe(roll, tmp_path / "rolls", adv)
    note = load_recipe(path)["_half_frame_detect"]
    assert "applied" not in note and note["failed"] == {"B": "failed", "C": "partial"}
    assert recipe["per_frame_overrides"] == {}
