from pathlib import Path

import pytest

from negmcp.config import REPO_ROOT, ConfigError, load_config
from negmcp.recipe import load_recipe


def test_precedence_cli_over_env_over_file_over_default(tmp_path, monkeypatch):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('workers = 3\niter_long_edge = 1800\nrefs_dir = "/file/refs"\n')
    monkeypatch.setenv("NEGMCP_ITER_LONG_EDGE", "2000")
    monkeypatch.setenv("NEGMCP_REFS_DIR", "/env/refs")
    cfg = load_config({"refs_dir": "/cli/refs"}, config_file=cfg_file)
    assert cfg.workers == 3  # file
    assert cfg.iter_long_edge == 2000  # env beats file
    assert cfg.refs_dir == Path("/cli/refs")  # CLI beats env
    assert cfg.preview_long_edge == 1024  # default


def test_derived_defaults_follow_their_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("NEGMCP_LOOK_DIR", str(tmp_path / "look"))
    monkeypatch.setenv("NEGMCP_CACHE_DIR", str(tmp_path / "cache"))
    cfg = load_config()
    assert cfg.house_look == tmp_path / "look" / "house_look.json"
    assert cfg.work_dir == tmp_path / "cache" / "work"


def test_unknown_keys_are_errors(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text("wrokers = 3\n")
    with pytest.raises(ConfigError, match="wrokers"):
        load_config(config_file=bad)
    with pytest.raises(ConfigError, match="nope"):
        load_config({"nope": 1})


def test_defaults_are_xdg_and_workspace(tmp_path, monkeypatch):
    for key in ("LOOK_DIR", "ROLLS_DIR", "NEG_ROOT", "CACHE_DIR"):
        monkeypatch.delenv(f"NEGMCP_{key}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    cfg = load_config()
    assert cfg.look_dir == tmp_path / "data" / "negmcp" / "look"
    assert cfg.rolls_dir == tmp_path / "data" / "negmcp" / "rolls"
    assert cfg.house_look == cfg.look_dir / "house_look.json"
    assert cfg.cache_dir == tmp_path / ".cache" / "negmcp"
    assert cfg.neg_root == tmp_path / "negmcp" / "neg" and cfg.refs_dir == tmp_path / "negmcp" / "refs"
    assert cfg.negpy_src == REPO_ROOT / "vendor" / "NegPy"


def test_relative_xdg_values_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("NEGMCP_LOOK_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert load_config().look_dir == tmp_path / ".local" / "share" / "negmcp" / "look"


def test_shipped_look_template_is_a_valid_base():
    from importlib import resources

    tpl = resources.files("negmcp") / "data" / "look_template" / "base_template.json"
    with resources.as_file(tpl) as path:
        recipe = load_recipe(Path(path))
    assert recipe["base"]["output_working_space"] is False
