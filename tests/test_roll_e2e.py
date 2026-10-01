"""The whole flow on a synthetic 35mm roll, through the CLI: init -> new -> batch -> qa -> fix ->
batch --final -> sidecars -> contact. Raws are synthetic DNGs named *.ARW (LibRaw reads the
content), refs are synthetic JPEGs; NegPy is the real vendored engine."""

import json
import sys

import numpy as np
from conftest import FIXTURES
from PIL import Image

from negmcp import _negpy
from negmcp.cli import main
from negmcp.config import get_config

sys.path.insert(0, str(FIXTURES))
from synthetic_raw import make_negative_dng  # noqa: E402

ROLL = "synth"
STEMS = ("S001", "S002", "S003")


def _refs(d):
    d.mkdir()
    rng = np.random.default_rng(0)
    for i in range(5):
        base = np.linspace(10, 240, 160)[None, :, None] * np.array([1.0, 0.97, 0.92])
        img = np.clip(base + rng.normal(0, 4, (120, 160, 3)) + 6 * i, 0, 255).astype(np.uint8)
        Image.fromarray(img).save(d / f"ref{i}.jpg", quality=92)


def test_synthetic_roll_end_to_end(tmp_path, monkeypatch, capsys):
    for key, sub in (("REFS_DIR", "refs"), ("REVIEW_ROOT", "review"), ("POS_ROOT", "pos")):
        monkeypatch.setenv(f"NEGMCP_{key}", str(tmp_path / sub))
    monkeypatch.setenv("NEGMCP_WORKERS", "2")
    get_config.cache_clear()
    _refs(tmp_path / "refs")
    roll_dir = tmp_path / "neg" / ROLL
    for i, stem in enumerate(STEMS):
        make_negative_dng(roll_dir / f"{stem}.ARW", exposure=0.1 * i)
    (roll_dir / "info.txt").write_text("film: synthetic 200\nformat: 35mm\nnegpy_crosstalk_profile: kodak_gold_200\n")

    assert main(["init"]) == 0
    assert main(["new", ROLL]) == 0
    recipe_path = tmp_path / "rolls" / f"{ROLL}_recipe.json"
    assert json.loads(recipe_path.read_text())["base"]["crosstalk_profile"] == "kodak_gold_200"

    assert main(["batch", ROLL, "--long-edge", "300"]) == 0
    review = tmp_path / "review" / ROLL
    assert sorted(p.stem for p in review.glob("*.jpg")) == list(STEMS)
    manifest = json.loads((review / "_manifest.json").read_text())
    assert manifest["frames"] and "_graded_negpy_version" not in json.loads(recipe_path.read_text())

    qa_json = tmp_path / "qa.json"
    assert main(["qa", ROLL, "--json", str(qa_json), "--long", "300"]) in (0, 1)  # 1 = HARD flags (synthetic)
    report = json.loads(qa_json.read_text())
    assert set(STEMS) <= set(report) and "_corridor" in report

    assert main(["fix", ROLL, "S002", "FF", '{"density": 0.9}']) == 0
    assert json.loads(recipe_path.read_text())["per_frame_overrides"]["S002"] == {"density": 0.9}

    assert main(["batch", ROLL, "--final"]) == 0
    assert json.loads(recipe_path.read_text())["_graded_negpy_version"] == _negpy.read_pin().version
    with Image.open(review / "S001.jpg") as im:
        assert im.size == (600, 400)

    assert main(["sidecars", ROLL]) == 0
    assert sorted(p.stem for p in roll_dir.glob("*.negpy")) == list(STEMS)
    assert main(["contact", ROLL]) == 0
    assert (tmp_path / "review" / f"{ROLL}_contact.jpg").is_file()
