import json
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from negmcp import corridor, metrics, refs
from negmcp.config import get_config

TASTE = "warm-neutral, white whites"


def _setup(tmp_path, monkeypatch, n=3):
    d = tmp_path / "refs"
    d.mkdir()
    for i in range(n):
        Image.new("RGB", (32, 24), (40 + 60 * i, 120, 200 - 50 * i)).save(d / f"r{i}.jpg")
    adv = tmp_path / "look"
    adv.mkdir()
    monkeypatch.setenv("NEGMCP_REFS_DIR", str(d))
    get_config.cache_clear()
    return d, adv / "house_look.json"


def test_derive_keeps_hand_written_fields_and_drops_v1(tmp_path, monkeypatch):
    _, house = _setup(tmp_path, monkeypatch)
    house.write_text(
        json.dumps(
            {
                "_note": "v1",
                "taste": TASTE,
                "user_extra": {"a": 1},
                "fingerprint": {"black_point": 0.05},
                "vinci_baseline_guidance": {"temp": 4},
            }
        )
    )
    st = refs.ensure()
    assert st.house_why == "not derived yet"  # a pre-hash (v1) file is stale
    got = json.loads(house.read_text())
    assert got["taste"] == TASTE and got["user_extra"] == {"a": 1}
    assert "vinci_baseline_guidance" not in got and "black_point" not in got["fingerprint"]
    assert got["refs_hash"] == st.refs_hash == st.corridor["refs_hash"]
    # house and corridor are the same measurement
    assert got["fingerprint"]["span"] == round(st.corridor["SPAN"]["dist"]["median"], 4)
    assert got["bands"]["gm"] == pytest.approx([st.corridor["GM"]["floor"], st.corridor["GM"]["ceil"]], abs=2e-4)
    assert (tmp_path / "cache").exists() and st.contact.is_file()


def test_refs_change_rewrites_both_targets_in_one_pass(tmp_path, monkeypatch):
    d, house = _setup(tmp_path, monkeypatch)
    refs.ensure()
    calls = []
    real = refs.measure_refs
    monkeypatch.setattr(refs, "measure_refs", lambda p: calls.append(p) or real(p))
    st = refs.ensure()
    assert (st.corridor_why, st.house_why, calls) == (None, None, [])
    before = json.loads(house.read_text())
    house.write_text(json.dumps({**before, "taste": TASTE}))
    Image.new("RGB", (32, 24), (250, 250, 250)).save(d / "bright.jpg")
    st = refs.ensure()
    assert (st.corridor_why, st.house_why, len(calls)) == ("refs changed", "refs changed", 1)
    after = json.loads(house.read_text())
    assert after["n_refs"] == st.corridor["n_refs"] == 4 and after["taste"] == TASTE
    assert after["fingerprint"]["white"] > before["fingerprint"]["white"]
    assert corridor.load_corridor()["refs_hash"] == after["refs_hash"] != before["refs_hash"]


def test_measure_change_makes_targets_stale(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    refs.ensure()
    monkeypatch.setattr(refs, "MEASURE", "next")
    st = refs.ensure()
    assert (st.corridor_why, st.house_why) == ("measure changed", "measure changed")


def test_only_the_stale_target_is_rewritten(tmp_path, monkeypatch):
    _, house = _setup(tmp_path, monkeypatch)
    refs.ensure()
    house.unlink()
    st = refs.ensure()
    assert (st.corridor_why, st.house_why) == (None, "missing") and house.is_file()


P3 = Path("/System/Library/ColorSync/Profiles/Display P3.icc")


def test_open_srgb_passes_srgb_through(tmp_path):
    plain, tagged = tmp_path / "plain.jpg", tmp_path / "srgb.jpg"
    img = Image.new("RGB", (8, 8), (200, 80, 40))
    img.save(plain)
    img.save(tagged, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    assert metrics.open_srgb(plain).getpixel((0, 0)) == metrics.open_srgb(tagged).getpixel((0, 0))


@pytest.mark.skipif(not P3.is_file(), reason="no Display P3 profile on this machine")
def test_open_srgb_converts_display_p3(tmp_path):
    p = tmp_path / "p3.jpg"
    Image.new("RGB", (8, 8), (200, 80, 40)).save(p, icc_profile=P3.read_bytes(), quality=100)
    r, g, b = metrics.open_srgb(p).getpixel((0, 0))
    assert r > 205 and b < 40  # P3 code values are less saturated than the same colour in sRGB
