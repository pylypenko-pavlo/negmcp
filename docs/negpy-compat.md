# NegPy compatibility and the pin

negmcp renders with NegPy's own engine — `ImageProcessor(use_gpu=False)` →
`run_pipeline(...)` → NegPy's export encode — and keeps only what the desktop app does not
have headless: iteration sizing, the `output_working_space` passthrough, crosstalk profiles
by file stem, rebate detection for frames without a crop (`render_crops`, used by the
corridor), roll autocrop glue and roll pooling (the app's versions are Qt workers).

## The pin

`vendor/NEGPY_PIN` is the single source of truth (`NEGPY_COMMIT`, `NEGPY_VERSION`);
`negmcp/_negpy.py` refuses to import a checkout whose `VERSION` / HEAD differ from it.

- `bash vendor/setup.sh` — clone / check out the pin.
- `negmcp start` — moves to the newest **release** tag (pre-releases ignored), installs
  missing render-path dependencies from NegPy's own `pyproject.toml` (uv), smoke-renders the
  first scan under `neg_root` in a subprocess and rolls checkout + pin back on failure.
- `negmcp start --no-update` — smoke the current pin only.

## What to check after a pin move

The smoke render proves the engine runs, not that it looks the same. In order:

1. **Glue**: NegPy APIs negmcp calls directly — `ImageProcessor._load_source_f32`,
   `run_pipeline`, `_export_pixels` / `buffer_to_pil`, `WorkspaceConfig.from_flat_dict`,
   `domain/migrations.py` (renamed / dropped keys), `GeometryProcessor`, batch autocrop and
   half-frame split helpers, crosstalk profile resolution. `pytest` covers the synthetic path.
2. **Defaults**: build your active recipes on both versions and diff the dataclasses —
   upstream changes defaults (`linear_raw`, `paper_dmin`, `cast_removal_strength` all moved
   in the past), which changes the look without any key changing. A field you care about
   should be explicit in your base.
3. **Pixels**: `NEGMCP_TEST_ARW=... python tests/fixtures/make_baseline.py` on the old pin
   before the move tells you the old hashes; after the move the smoke test skips until you
   regenerate. For a measured comparison render the same frames on both pins and compare
   `negmcp.metrics.fingerprint` per axis.
4. **Look**: `negmcp qa <roll>` on rolls graded on the old version — the ENGINE line and the
   corridor show how many frames moved, and in which axes. Re-tune the base only if the move
   is systematic and away from your refs.

## History (engine changes that reached the render path)

- **0.34 → 0.53**: working space ProPhoto (ROMM) → Adobe RGB (1998); a real
  sensor→working colour matrix before the bounds analysis (0.34 fed raw sensor RGB into
  `log10`). Same recipe: deeper blacks, wider tonal span, slightly brighter and more
  saturated; white balance essentially unchanged. Defaults moved: `linear_raw` and
  `paper_dmin` true → false. Migrations centralised in `domain/migrations.py`
  (`manual_crop_rect` → `crop_rect`, `color_separation` → `crosstalk_strength`,
  `true_black` → `paper_black` with inverted polarity, …). `get_manual_rect_coords` reads
  the rect in the transformed frame. Crosstalk display names gained " (approx)" — address
  profiles by file stem.
- **0.53 → 0.54**: bit-identical on the C-41 path (`should_fold_camera_wb` differs from
  `effective_linear_raw` only with `narrowband_scan: true`).
- **0.54 → 0.62**: decode re-synced with the app (fixed white level, calibrated saturation
  limit, demosaic from `process.demosaic_export`, highlight gates); `cast_removal_strength`
  default 0.5 → 1.0; print-tone / anchor-meter rework. Same recipe prints warmer, less green,
  a brighter key, slightly softer blacks. Slides moved to `negpy/features/transparency/*`
  (routed by the render path — a hard-wired print path renders a slide as a negative) and
  Cast Removal now acts on slides; set `cast_removal_strength` in `film_deltas.json →
  _transparency` if your slides go yellow. `e6_normalize` dropped.
- **Since 0.62** negmcp calls the engine itself instead of reproducing its stages, so
  geometry (fine rotation, distortion, keystone), the Lab stage and the slide path follow
  upstream automatically.

## Crops

- **35mm**: `negmcp autocrop` = the app's "Auto Crop All" headless (`batch_autocrop`
  candidates → `resolve_roll_crops` over the roll, `manual_crop_rect` + `fine_rotation`).
  In testing it matched hand-set crops closely (median IoU ≈ 0.98) and erred on the tight
  side.
- **Half-frame**: NegPy's per-half crop (split profile + single-frame autocrop) runs to the
  outer film extent and can keep bright or black rebate slivers and pull a light-struck
  leader edge into the frame; the per-side default crop did not. So half-frame rolls stay on
  `crop.DEFAULT_CROP` (`autocrop` is a dry run there) and the split detection is recorded as a
  note only.
- `crop_from_auto: true` (per-render engine autocrop) is allowed on 35mm, refused on
  half-frame and next to a crop rect: the engine re-detects on the whole scan and drops the
  rect.

## Output colour

`output_working_space: false` — NegPy's export: quantise + ICC working → target (relative
colorimetric + black-point compensation). `true` — the working-space buffer written as-is
and only *tagged* with the target profile: what NegPy builds before 0.53 exported. A batch
records the pixel space in `_manifest.json`; `negmcp qa` and the MCP `fingerprint` tool
convert to sRGB from it before measuring.

## Other dependencies

`mcp` 2.x removed `mcp.server.fastmcp`; negmcp pins `mcp<2`.
