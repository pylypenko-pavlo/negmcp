import json

import pytest
from conftest import BASE

from negmcp import batch
from negmcp.config import get_config
from negmcp.recipe import GRADED_VERSION_KEY, RecipeError, engine_drift, stamp_graded_version, validate_recipe


def test_graded_version_key_is_allowed_and_typed():
    validate_recipe({"base": BASE, GRADED_VERSION_KEY: "0.62.0"})
    with pytest.raises(RecipeError, match=GRADED_VERSION_KEY):
        validate_recipe({"base": BASE, GRADED_VERSION_KEY: 62})


def test_stamp_writes_only_the_version(make_recipe):
    path = make_recipe({"_note": "x", "base": BASE, "per_frame_overrides": {"A": {"grade": 100}}})
    stamp_graded_version(path, "0.62.0")
    r = json.loads(path.read_text())
    assert r[GRADED_VERSION_KEY] == "0.62.0"
    assert r["per_frame_overrides"] == {"A": {"grade": 100}} and r["_note"] == "x"


def test_engine_drift_messages():
    assert engine_drift({GRADED_VERSION_KEY: "0.62.0"}, "0.62.0") is None
    assert "graded on NegPy 0.54.0, rendering on 0.62.0" in engine_drift({GRADED_VERSION_KEY: "0.54.0"}, "0.62.0")
    assert "never finalised" in engine_drift({}, "0.62.0")


@pytest.fixture
def fake_roll(tmp_path, monkeypatch):
    """A 2-frame full-frame roll whose render is stubbed (no NegPy work)."""
    from PIL import Image

    roll = tmp_path / "neg" / "r1"
    roll.mkdir(parents=True)
    (roll / "info.txt").write_text("film: test\nformat: 35mm\n")
    for s in ("F1", "F2"):
        (roll / f"{s}.ARW").write_bytes(b"")
    adv = tmp_path / "rolls"
    adv.mkdir()
    recipe = adv / "r1_recipe.json"
    recipe.write_text(json.dumps({"base": BASE, "per_frame_overrides": {}, GRADED_VERSION_KEY: "0.1.0"}))

    def fake_render_task(task):
        Image.new("RGB", (8, 8), (128, 128, 128)).save(task.out)
        return ("ok", task.out, {"metered_anchor": 0.5})

    monkeypatch.setattr(batch, "_render_task", fake_render_task)
    monkeypatch.setattr(batch.exif, "batch_exif", lambda info, arw: b"")
    monkeypatch.setattr(batch.mp, "get_context", lambda _: _InlineCtx())
    return roll, recipe


class _InlineCtx:
    def Pool(self, n):  # noqa: N802 - mirrors multiprocessing API
        return _InlinePool()


class _InlinePool:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        return [fn(i) for i in items]


def test_final_batch_stamps_and_warns(fake_roll, tmp_path):
    roll, recipe = fake_roll
    cfg = get_config()
    current = batch._negpy.installed_version()
    res = batch.run_batch(cfg, str(roll), out_dir=tmp_path / "review")
    assert res.engine_warning and "0.1.0" in res.engine_warning and res.stamped is None
    assert json.loads(recipe.read_text())[GRADED_VERSION_KEY] == "0.1.0"  # iteration never stamps

    res = batch.run_batch(cfg, str(roll), out_dir=tmp_path / "review", final=True)
    assert res.stamped == current
    assert json.loads(recipe.read_text())[GRADED_VERSION_KEY] == current
    manifest = json.loads(res.manifest.read_text())
    assert manifest["graded_negpy_version"] == "0.1.0" and "0.1.0" in manifest["engine_warning"]
    assert manifest["output_working_space"] is True

    res = batch.run_batch(cfg, str(roll), out_dir=tmp_path / "review")
    assert res.engine_warning is None


def test_recipe_path_falls_back_to_closed(tmp_path):
    cfg = get_config()
    closed = cfg.rolls_dir / "closed"
    closed.mkdir(parents=True)
    (closed / "old_recipe.json").write_text("{}")
    assert cfg.recipe_path("old") == closed / "old_recipe.json"
    (cfg.rolls_dir / "old_recipe.json").write_text("{}")
    assert cfg.recipe_path("old") == cfg.rolls_dir / "old_recipe.json"
    assert cfg.recipe_path("missing") == cfg.rolls_dir / "missing_recipe.json"
