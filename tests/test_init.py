import json
import tomllib

from negmcp.cli import main
from negmcp.config import get_config
from negmcp.look import TEMPLATE_FILES
from negmcp.recipe import load_recipe, new_recipe


def test_init_creates_the_look_saves_the_config_and_keeps_existing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NEGMCP_LOOK_DIR")
    look = tmp_path / "mylook"
    assert main(["init", "--look-dir", str(look)]) == 0
    assert sorted(p.name for p in look.iterdir()) == sorted(TEMPLATE_FILES)
    saved = tomllib.loads((tmp_path / "no-config.toml").read_text())
    assert saved["look_dir"] == str(look.resolve())
    assert (tmp_path / "rolls").is_dir()

    (look / "house_look.json").write_text(json.dumps({"taste": "mine"}))
    capsys.readouterr()
    assert main(["init"]) == 0
    assert "kept" in capsys.readouterr().out
    assert json.loads((look / "house_look.json").read_text()) == {"taste": "mine"}


def test_new_recipe_from_the_shipped_template(tmp_path, monkeypatch):
    monkeypatch.delenv("NEGMCP_LOOK_DIR")
    look = tmp_path / "look2"
    main(["init", "--look-dir", str(look)])
    roll = tmp_path / "neg" / "r1"
    roll.mkdir(parents=True)
    (roll / "info.txt").write_text("film: any\nformat: 35mm\nnegpy_crosstalk_profile: kodak_gold_200\n")
    cfg = get_config()
    path, recipe = new_recipe(roll, cfg.rolls_dir, cfg.look_dir)
    assert recipe["base"]["crosstalk_profile"] == "kodak_gold_200"
    assert recipe["_stock_deltas_applied"] == {
        "profile": "kodak_gold_200",
        "known_stock": False,
        "pushed": False,
        "deltas": {},
    }
    assert load_recipe(path)["base"]["output_working_space"] is False
