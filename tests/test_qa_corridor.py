import json

from PIL import Image

from negmcp import qa
from negmcp.config import get_config


def _img(path, rgb, gradient=True):
    im = Image.new("RGB", (64, 48), rgb)
    if gradient:
        for x in range(64):
            for y in range(48):
                v = int(255 * x / 63)
                im.putpixel((x, y), (min(255, v + rgb[0] // 4), v, max(0, v - rgb[2] // 8)))
    im.save(path)


def test_qa_runs_corridor_step_and_reads_manifest(tmp_path, monkeypatch, capsys):
    refs = tmp_path / "refs"
    refs.mkdir()
    for i in range(5):
        _img(refs / f"ref{i}.jpg", (40 * i, 120, 200 - 30 * i))
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(refs))
    get_config.cache_clear()
    review = tmp_path / "review" / "r1"
    review.mkdir(parents=True)
    _img(review / "A_L.jpg", (60, 120, 140))
    Image.new("RGB", (64, 48), (128, 128, 128)).save(review / "B_L.jpg")  # flat grey: span 0 -> out
    (review / "_manifest.json").write_text(
        json.dumps(
            {
                "pixel_color_space": "sRGB",
                "negpy_version": "0.62.0",
                "graded_negpy_version": "0.54.0",
                "negpy_metrics": {"A_L": {"metered_anchor": 0.5}, "B_L": {"metered_anchor": 0.4}},
            }
        )
    )
    outj = tmp_path / "qa.json"
    qa.run(review, 64, outj)
    text = capsys.readouterr().out
    assert "CORRIDOR (refs n=5, recalibrated: missing" in text
    assert "B_L: span 0.00<" in text
    assert "ENGINE: roll graded on NegPy 0.54.0, now 0.62.0" in text
    assert "metered_anchor" in text
    report = json.loads(outj.read_text())
    assert "span" in report["B_L"]["corridor"]["out"]
    assert report["_corridor"]["frames"] == 2


def test_qa_fails_loudly_without_refs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(tmp_path / "missing"))
    get_config.cache_clear()
    review = tmp_path / "rv"
    review.mkdir()
    Image.new("RGB", (32, 32), (100, 100, 100)).save(review / "A.jpg")
    assert qa.run(review, 32) == 2
    assert "CORRIDOR STEP FAILED" in capsys.readouterr().out
