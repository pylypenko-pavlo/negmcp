"""render.py drives NegPy's own engine: geometry, render path and crosstalk come from upstream.

Synthetic buffers go straight into ``render._run_engine`` (ImageProcessor.run_pipeline on the
CPU), so these run without any raw; the last test renders a real slide when NEGMCP_TEST_SLIDE_ARW is set.
"""

import math
import os
from pathlib import Path

import numpy as np
import pytest

from negmcp import metrics, render

H, W = 400, 600


def _line_image(row: int = H // 2) -> np.ndarray:
    img = np.full((H, W, 3), 0.05, dtype=np.float32)
    img[row - 2 : row + 3, :, :] = 0.6
    return img


def _line_angle_deg(buf: np.ndarray) -> float:
    """Angle of the one bright-or-dark line across the frame (CCW positive, image y down)."""
    luma = buf[..., :3].mean(axis=2)
    dev = np.abs(luma - np.median(luma, axis=0, keepdims=True))
    cols = np.arange(W // 4, 3 * W // 4)
    rows = dev[:, cols].argmax(axis=0)
    slope = np.polyfit(cols, rows, 1)[0]
    return -math.degrees(math.atan(slope))


@pytest.mark.parametrize("angle", [0.0, 3.0, -2.0])
def test_fine_rotation_is_applied_by_the_engine(angle):
    cfg = render.build_workspace_config({"process_mode": "C41", "fine_rotation": angle})
    buf, _ = render._run_engine("synthetic", _line_image(), cfg)
    assert buf.shape[:2] == (H, W)  # same canvas: NegPy rotates in place, edges replicated
    assert _line_angle_deg(buf) == pytest.approx(angle, abs=0.25)


def test_crop_rect_is_read_in_the_transformed_frame():
    # rotation=1 turns the 400x600 scan into 600x400; the left half of THAT frame is 600x200.
    cfg = render.build_workspace_config({"process_mode": "C41", "rotation": 1, "crop_rect": [0.0, 0.0, 0.5, 1.0]})
    buf, _ = render._run_engine("synthetic", _line_image(), cfg)
    assert buf.shape[:2] == (W, H // 2)


def _two_patch_image() -> np.ndarray:
    img = np.empty((H, W, 3), dtype=np.float32)
    img[:, : W // 2] = 0.6  # bright on the light table
    img[:, W // 2 :] = 0.05
    return img


@pytest.mark.parametrize(
    ("mode", "inverts"),
    [("C41", True), ("B&W", True), ("Transparency", False)],
)
def test_render_path_follows_the_process_mode(mode, inverts):
    """A negative is inverted (print path); a slide is shown as captured (transfer path)."""
    cfg = render.build_workspace_config({"process_mode": mode, "e6_normalize": False})
    buf, m = render._run_engine("synthetic", _two_patch_image(), cfg)
    left, right = float(buf[:, : W // 2].mean()), float(buf[:, W // 2 :].mean())
    assert (left < right) is inverts


def test_slide_uses_the_fixed_transfer_window():
    from negpy.features.transparency.logic import transfer_bounds

    floors, ceils = transfer_bounds()
    cfg = render.build_workspace_config({"process_mode": "Transparency", "e6_normalize": False})
    _, m = render._run_engine("synthetic", _two_patch_image(), cfg)
    assert tuple(m["final_bounds"].floors) == tuple(floors)
    assert tuple(m["final_bounds"].ceils) == tuple(ceils)


def test_crosstalk_profile_resolves_by_stem_and_by_display_name():
    from negpy.kernel.system.paths import get_resource_path
    from negpy.services.assets.crosstalk import CrosstalkProfiles

    path = os.path.join(get_resource_path("crosstalk"), "kodak_gold_200.toml")
    display, matrix = CrosstalkProfiles._parse_file(path)
    assert render.resolve_crosstalk_profile("kodak_gold_200") == matrix
    assert render.resolve_crosstalk_profile(display) == matrix
    assert render.resolve_crosstalk_profile("Generic C41") is None
    assert render.resolve_crosstalk_profile("no_such_stock") is None


SLIDE = Path(os.environ.get("NEGMCP_TEST_SLIDE_ARW", "/nonexistent")).expanduser()


@pytest.mark.render
@pytest.mark.skipif(not SLIDE.is_file(), reason="set NEGMCP_TEST_SLIDE_ARW to a raw scan of an E-6 slide")
def test_slide_arw_renders_positive_not_inverted():
    """0.62 moved the slide path out of the exposure processors; a print-path render of a
    slide comes out as an inverted negative (key ~0.01). The engine renders it as the slide."""
    im = render.render(
        str(SLIDE),
        {"process_mode": "Transparency", "e6_normalize": False, "output_working_space": False},
        long_edge=600,
    )
    assert metrics.fingerprint(im)["key"] > 0.3
