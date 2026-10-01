import json

from PIL import Image

from negmcp import qa
from negmcp.config import get_config


def _grad(path, size=(64, 48), tint=(0, 0, 0)):
    w, h = size
    im = Image.new("RGB", size)
    for x in range(w):
        v = int(255 * x / (w - 1))
        for y in range(h):
            im.putpixel((x, y), tuple(min(255, max(0, v + t)) for t in tint))
    im.save(path)


def _refs(tmp_path, monkeypatch):
    refs = tmp_path / "refs"
    refs.mkdir()
    for i in range(5):
        _grad(refs / f"ref{i}.jpg", tint=(10 * i, 0, -10 * i))
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(refs))
    get_config.cache_clear()


def test_match_by_stem_half_split_and_suffix(tmp_path):
    pos = tmp_path / "pos"
    pos.mkdir()
    _grad(pos / "DSC1.jpg", (96, 64))  # whole landscape frame -> our _L/_R halves
    _grad(pos / "DSC2.jpg", (32, 64))  # portrait: already a half, must NOT be split
    _grad(pos / "B.jpg")
    _grad(pos / "roll_Frame007.jpg")
    _grad(pos / "Roll_Frame009.jpg")  # claimed by two of ours by suffix -> ambiguous -> neither
    _grad(pos / "_contact.jpg")
    stems = ["DSC1_L", "DSC1_R", "DSC2_L", "B", "Frame007", "x_Frame009", "y_Frame009", "DSC9"]
    matched, unused = qa.match_approved(stems, pos)
    assert matched["DSC1_L"] == (pos / "DSC1.jpg", "L")
    assert matched["DSC1_R"] == (pos / "DSC1.jpg", "R")
    assert matched["B"] == (pos / "B.jpg", None)
    assert matched["Frame007"] == (pos / "roll_Frame007.jpg", None)
    assert {"DSC2_L", "x_Frame009", "y_Frame009", "DSC9"}.isdisjoint(matched)
    assert unused == ["DSC2", "Roll_Frame009"]


def test_approved_frame_delta_sign_and_threshold():
    base = Image.new("RGB", (32, 32), (100, 100, 100))
    brighter = Image.new("RGB", (32, 32), (160, 160, 160))
    rec = qa.approved_frame(brighter, base)
    assert rec["delta"]["key"] > qa.APPROVED_T["key"]  # ours brighter -> positive delta
    assert "key" in rec["over"]
    assert qa.approved_frame(base, base)["over"] == []


def test_qa_reports_approved_and_keeps_exit_code(tmp_path, monkeypatch, capsys):
    _refs(tmp_path, monkeypatch)
    review = tmp_path / "review" / "r1"
    review.mkdir(parents=True)
    pos = tmp_path / "finals"
    pos.mkdir()
    _grad(review / "A.jpg")
    _grad(pos / "A.jpg")
    _grad(review / "B.jpg", tint=(60, 60, 60))  # brighter than its final
    _grad(pos / "B.jpg")
    _grad(review / "C.jpg")  # no final
    outj = tmp_path / "qa.json"
    rc_without = qa.run(review, 64, use_approved=False)
    capsys.readouterr()
    assert qa.run(review, 64, outj, approved=pos) == rc_without  # REVIEW only, never HARD
    text = capsys.readouterr().out
    assert "--- APPROVED (finals" in text and "matched 2/3" in text
    assert "1 of our frames have no approved final" in text
    report = json.loads(outj.read_text())
    assert report["approved"]["frames"] == 2 and report["approved"]["unmatched"] == ["C"]
    assert report["A"]["approved"]["over"] == []
    assert "key" in report["B"]["approved"]["over"]


def test_qa_without_approved_finals(tmp_path, monkeypatch, capsys):
    _refs(tmp_path, monkeypatch)
    monkeypatch.setenv("NEGMCP_POS_ROOT", str(tmp_path / "pos"))  # pos_root/<roll> absent
    get_config.cache_clear()
    review = tmp_path / "review" / "r2"
    review.mkdir(parents=True)
    _grad(review / "A.jpg")
    outj = tmp_path / "qa.json"
    qa.run(review, 64, outj)
    out = capsys.readouterr().out
    assert f"no approved finals ({tmp_path / 'pos' / 'r2'} missing)" in out
    assert json.loads(outj.read_text())["approved"]["frames"] == 0
    (tmp_path / "other").mkdir()
    _grad(tmp_path / "other" / "Z.jpg")
    qa.run(review, 64, approved=tmp_path / "other")
    assert "none of 1 files match our 1 frame names" in capsys.readouterr().out
    qa.run(review, 64, use_approved=False)
    assert "APPROVED: off" in capsys.readouterr().out
