import json

from conftest import BASE

from negmcp.recipe import new_recipe


def _setup(tmp_path, info: str):
    roll = tmp_path / "neg" / "s1"
    roll.mkdir(parents=True)
    (roll / "info.txt").write_text(info)
    adv = tmp_path / "look"
    adv.mkdir()
    (adv / "base_template.json").write_text(json.dumps({"base": BASE}))
    (adv / "film_deltas.json").write_text(
        json.dumps({"_transparency": {"cast_removal_strength": 0.0}, "_push": {"_default": {"luma_range_clip": 2}}})
    )
    return roll, adv


def test_new_slide_roll_gets_transparency_mode_and_slide_deltas(tmp_path):
    roll, adv = _setup(
        tmp_path, "film: e6 slide\nformat: 35mm\nprocess_mode: Transparency (E-6)\npush: none (native)\n"
    )
    _, recipe = new_recipe(roll, tmp_path / "rolls", adv)
    base = recipe["base"]
    assert base["process_mode"] == "Transparency"
    assert base["crosstalk_profile"] is None
    assert base["cast_removal_strength"] == 0.0
    assert recipe["_stock_deltas_applied"]["pushed"] is False


def test_push_none_is_not_a_push(tmp_path):
    info = "film: gold\nformat: 35mm\nnegpy_crosstalk_profile: kodak_gold_200\npush: none\n"
    roll, adv = _setup(tmp_path, info)
    _, recipe = new_recipe(roll, tmp_path / "rolls", adv)
    assert recipe["_stock_deltas_applied"]["pushed"] is False
