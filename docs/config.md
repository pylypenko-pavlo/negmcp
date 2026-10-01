# Configuration

Precedence, highest first:

1. `negmcp -c KEY=VALUE` (repeatable; also `--log-level`) — exported as `NEGMCP_<KEY>` so
   spawned render workers resolve the same values;
2. environment `NEGMCP_<KEY>` (e.g. `NEGMCP_NEG_ROOT=/data/scans`);
3. the config file: `--config FILE`, else `NEGMCP_CONFIG`, else
   `$XDG_CONFIG_HOME/negmcp/config.toml` (`~/.config/negmcp/config.toml`);
4. built-in defaults.

The file is flat TOML; unknown keys are an error. Template: `negmcp.example.toml`.
`negmcp start` prints every path with its source. `negmcp init --look-dir DIR --rolls-dir DIR`
and `negmcp start --refs DIR` write those keys into the file for you.

## Keys

| Key | Default | What lives there |
|---|---|---|
| `negpy_src` | `<repo>/vendor/NegPy` | the pinned NegPy checkout (`bash vendor/setup.sh`) |
| `look_dir` | `$XDG_DATA_HOME/negmcp/look` (`~/.local/share/negmcp/look`) | `base_template.json`, `film_deltas.json`, `house_look.json` — created by `negmcp init` |
| `rolls_dir` | `$XDG_DATA_HOME/negmcp/rolls` | `<roll>_recipe.json`, `closed/`, `<roll>_overrides.json` |
| `house_look` | `<look_dir>/house_look.json` | taste + refs-derived target |
| `neg_root` | `~/negmcp/neg` | `<roll>/` folders of raw scans + `info.txt` |
| `refs_dir` | `~/negmcp/refs` | reference images of the look you want |
| `review_root` | `~/negmcp/review` | `<roll>/` = `batch(recipe)`; regenerated, never edited |
| `pos_root` | `~/negmcp/pos` | `<roll>/` = approved finals (optional; `qa` APPROVED) |
| `edits_db` | `~/Documents/NegPy/edits.db` | the NegPy desktop app's database (`replay`; opened read-only) |
| `cache_dir` | `$XDG_CACHE_HOME/negmcp` (`~/.cache/negmcp`) | `state.json`, recipe locks, pool cache |
| `work_dir` | `<cache_dir>/work` | MCP renders, contact sheets, corridor output |
| `iter_long_edge` | `2200` | iteration size: long edge of the cropped output |
| `preview_long_edge` | `1024` | MCP image payloads, contact cells |
| `workers` | cpu count − 2 | render processes |
| `log_level` | `INFO` | `DEBUG` also shows tracebacks for expected CLI errors |

Relative `XDG_*` values are ignored (XDG base-directory rule). Keep `look_dir` and
`rolls_dir` outside the repository: they are your data. A cloud-synced folder is fine — the
recipe lock lives in `cache_dir`, not next to the recipe.

## Roll names and paths

Commands that take a roll accept a name (`neg_root/<roll>`, recipe `rolls_dir/<roll>_recipe.json`,
falling back to `rolls_dir/closed/`) or an explicit path (anything with a `/` or `~`).

## Test-only environment

| Variable | Used by |
|---|---|
| `NEGMCP_TEST_ARW` | `tests/fixtures/make_baseline.py` + `tests/test_render_smoke.py`: your own scans (`os.pathsep`-separated) for a pixel-exact render baseline |
| `NEGMCP_TEST_BASELINE_DIR` | where that baseline lives (default `~/.cache/negmcp/test-baseline`) |
| `NEGMCP_TEST_SLIDE_ARW` | a raw scan of an E-6 slide for the slide render test |
