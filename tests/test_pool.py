import json

from conftest import BASE

from negmcp.exif import RollFormat
from negmcp.pool import OUTLIER_OVERRIDE, FrameBounds, apply_pool, pool_frames
from negmcp.recipe import load_recipe

AXIS = ((-1.35, -1.47, -1.87), (-1.6, -1.7, -2.1), None, 0.8)


def _frames():
    base = [
        FrameBounds(f"F{i}_L", (-1.75 + 0.01 * i, -1.96, -2.45), (-0.95, -0.93, -1.29 - 0.01 * i), AXIS)
        for i in range(4)
    ]
    # luma-free colour far off the others (a blank / mis-cropped half): an outlier
    odd = FrameBounds("F9_R", (-1.0, -1.96, -3.2), (-0.2, -0.93, -2.0), AXIS)
    return [*base, odd]


def test_pool_flags_colour_outlier_and_keeps_luma_per_frame():
    res = pool_frames(_frames(), [])
    assert res.outliers == ["F9_R"]
    upd = res.base_update()
    assert upd["use_color_average"] is True and upd["use_luma_average"] is False
    assert upd["use_cast_average"] is (res.axis is not None)
    json.dumps(upd)  # recipe-serialisable


def test_apply_pool_writes_base_and_outlier_overrides_idempotently(make_recipe):
    path = make_recipe({"base": BASE, "per_frame_overrides": {"F1": {"L": {"wb_yellow": 0.02}}}})
    res = pool_frames(_frames(), [])
    apply_pool(path, res, RollFormat.HALF)
    r = load_recipe(path, fmt=RollFormat.HALF)
    assert r["base"]["use_color_average"] is True and len(r["base"]["locked_floors"]) == 3
    assert {k: r["per_frame_overrides"]["F9"]["R"][k] for k in OUTLIER_OVERRIDE} == OUTLIER_OVERRIDE
    assert r["per_frame_overrides"]["F1"]["L"] == {"wb_yellow": 0.02}

    res.outliers = []  # a re-pool with no outliers clears the old outlier overrides
    apply_pool(path, res, RollFormat.HALF)
    r = load_recipe(path, fmt=RollFormat.HALF)
    assert not any(k in r["per_frame_overrides"]["F9"]["R"] for k in OUTLIER_OVERRIDE)


def test_analyze_three_synthetic_halves(tmp_path):
    import sys

    from conftest import FIXTURES

    from negmcp.crop import DEFAULT_CROP
    from negmcp.pool import analyze_task

    sys.path.insert(0, str(FIXTURES))
    from synthetic_raw import make_negative_dng

    raws = [make_negative_dng(tmp_path / f"S{i}.dng", exposure=0.1 * i) for i in range(2)]
    cfg = {
        "process_mode": "C41",
        "crosstalk_profile": "kodak_gold_200",
        "output_working_space": False,
    }
    jobs = [
        (f"{raw.stem}_{side}", str(raw), {**cfg, "manual_crop_rect": DEFAULT_CROP[side]})
        for raw, side in ((raws[0], "L"), (raws[0], "R"), (raws[1], "L"))
    ]
    frames = [analyze_task(j) for j in jobs]
    assert all(isinstance(f, FrameBounds) for f in frames), frames
    res = pool_frames(frames, [])
    assert all(f < c for f, c in zip(res.floors, res.ceils, strict=True))
