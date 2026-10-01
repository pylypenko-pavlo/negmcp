# negmcp MCP tools

`negmcp serve` runs a FastMCP stdio server. Register it with your MCP client as
`command: <venv>/bin/negmcp`, `args: ["serve"]` (Claude Code: `examples/claude/mcp.json`, or
`claude mcp add negmcp -- <venv>/bin/negmcp serve`). Paths come from the negmcp config
(`docs/config.md`); no environment variables are needed.

Every flat config must carry `output_working_space` (a recipe `base` does): `true` =
working-space passthrough, `false` = colour-managed export.

| Tool | What it does |
|---|---|
| `render_frame(arw_path, config, full_res=False)` | Render one ARW. Iteration size by default: the OUTPUT crop's long edge = `iter_long_edge` (2200), downscaled before the tone math; `full_res=True` renders full size. Returns a `preview_long_edge` (1024) JPEG + fingerprint line; the render is saved to `<work_dir>/<stem>.jpg`. |
| `render_roll(roll_dir, config, per_frame=None, recipe_path=None)` | Every ARW in the folder (whole scan, no L/R split) at `preview_long_edge` -> labelled contact sheet `<work_dir>/<roll>_contact.jpg`. With `recipe_path`, warns when the recipe's `_graded_negpy_version` differs from the running NegPy. A preview: never stamps the version (only `negmcp batch --final` does). |
| `analyze_frame(arw_path, config)` | NegPy `NormalizationProcessor` metrics: floors/ceils, metered_anchor, textural_range, every other scalar in `context.metrics`. |
| `fingerprint(image_path)` | `negmcp.metrics.fingerprint` of any image in sRGB (a batch folder's `_manifest.json` `pixel_color_space` wins, as in `negmcp qa`; else the embedded ICC) + signed delta to the `house_look.json` median per axis, `!` = outside the refs p10..p90. `house_look.json` is derived by `negmcp refs` / `negmcp start` from the same refs measurement as the QA corridor. |
| `write_sidecar(roll_dir, configs)` | `{stem: flat_config}` -> `<stem>.negpy` next to the ARW, serialized by NegPy's own `WorkspaceConfig.to_dict()` (atomic write). |
| `write_sidecars(recipe_path)` | All sidecars of a full-frame roll straight from its recipe (base + per-frame override, skips skipped). Refuses half-frame rolls: a NegPy sidecar is one per ARW and cannot hold two L/R crops. |

Flat config keys are NegPy's (`WorkspaceConfig.from_flat_dict` + `domain/migrations.py`
legacy renames such as `manual_crop_rect` -> `crop_rect`, `color_separation` ->
`crosstalk_strength`). negmcp-only keys (`output_working_space`, `crop_inset`) are stripped
before NegPy sees the dict.
