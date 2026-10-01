import json
from pathlib import Path

import pytest

from negmcp.config import get_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No user config file, cache/locks/work under tmp; NegPy stays the repo's vendor checkout."""
    monkeypatch.setenv("NEGMCP_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("NEGMCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NEGMCP_LOOK_DIR", str(tmp_path / "look"))
    monkeypatch.setenv("NEGMCP_ROLLS_DIR", str(tmp_path / "rolls"))
    monkeypatch.setenv("NEGMCP_NEG_ROOT", str(tmp_path / "neg"))
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def make_recipe(tmp_path):
    def _make(data: dict, name: str = "roll_recipe.json") -> Path:
        p = tmp_path / name
        p.write_text(json.dumps(data, indent=2))
        return p

    return _make


BASE = {
    "process_mode": "C41",
    "auto_exposure": True,
    "color_separation": 1.05,  # legacy key, migrated by NegPy -> must be accepted
    "wb_magenta": -0.06,
    "output_working_space": True,
}
