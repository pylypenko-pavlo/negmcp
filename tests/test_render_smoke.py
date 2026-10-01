"""Render smoke tests.

Synthetic: a generated Bayer DNG (tests/fixtures/synthetic_raw.py) goes through the real
NegPy engine — runs everywhere. Real scans: set ``NEGMCP_TEST_ARW`` and write a local
baseline with ``tests/fixtures/make_baseline.py``; the pixel SHA-256 of each render is then
compared with it (skipped when either is missing or the pin moved past the baseline).
"""

import json
import logging
import sys

import numpy as np
import pytest
from conftest import FIXTURES

from negmcp import _negpy, metrics, render
from negmcp.crop import DEFAULT_CROP

sys.path.insert(0, str(FIXTURES))
from make_baseline import CONFIG, baseline_path, file_sha256, render_entry, scan_paths  # noqa: E402
from synthetic_raw import make_negative_dng  # noqa: E402

PIN = _negpy.read_pin()

pytestmark = pytest.mark.render


@pytest.fixture(scope="module")
def negative(tmp_path_factory):
    return make_negative_dng(tmp_path_factory.mktemp("raw") / "S001.dng")


def test_long_edge_for_output_matches_decoded_shape():
    for side in ("L", "R"):
        assert render.long_edge_for_output((4024, 6024), DEFAULT_CROP[side], 2200) == 4017


@pytest.mark.parametrize("ws", [False, True])
def test_synthetic_negative_is_inverted_and_tagged(negative, ws, caplog):
    with caplog.at_level(logging.WARNING):
        im = render.render(str(negative), {**CONFIG, "output_working_space": ws}, output_long_edge=300)
    assert im.size == (300, 200)
    assert im.info.get("icc_profile")
    luma = np.asarray(im.convert("L"), dtype=np.float32) / 255
    h, w = luma.shape
    dense_patch, thin_edge = luma[: h // 2 - 5, : w // 3 - 5].mean(), luma[h // 2 + 5 :, : w // 3 - 5].mean()
    assert dense_patch > 0.6 > 0.3 > thin_edge  # a dense negative prints bright, a thin one dark
    assert metrics.fingerprint(im)["span"] > 0.5
    assert "Dropping unknown config keys" not in caplog.text


def test_synthetic_render_is_deterministic_and_full_res_keeps_size(negative):
    cfg = {**CONFIG, "output_working_space": False}
    a = render.render(str(negative), cfg, output_long_edge=300)
    b = render.render(str(negative), cfg, output_long_edge=300)
    assert np.array_equal(np.asarray(a), np.asarray(b))
    assert render.render(str(negative), cfg).size == (600, 400)


def test_render_requires_explicit_mode(negative):
    with pytest.raises(render.MissingOutputModeError):
        render.render(str(negative), dict(CONFIG), output_long_edge=300)


@pytest.mark.parametrize("arw", scan_paths(), ids=lambda p: p.name)
@pytest.mark.parametrize("ws", [False, True])
def test_real_scan_is_bit_identical_to_local_baseline(arw, ws):
    path = baseline_path(PIN.version)
    if not path.is_file():
        pytest.skip(f"no local baseline for NegPy {PIN.version}: run tests/fixtures/make_baseline.py")
    baseline = json.loads(path.read_text())
    expected = baseline["frames"].get(f"{arw.name}_ws{int(ws)}")
    if baseline["negpy"] != PIN.version or expected is None or expected["arw_sha256"] != file_sha256(arw):
        pytest.skip(f"{path} was made for another pin or scan: re-run tests/fixtures/make_baseline.py")
    got = render_entry(arw, ws)
    assert got["size"] == expected["size"]
    assert got["sha256"] == expected["sha256"]
