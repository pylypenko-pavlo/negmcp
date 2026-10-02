# negmcp

Automation around [NegPy](https://github.com/marcinz606/NegPy) for people who already
convert colour negatives in NegPy and work in Claude Code. negmcp is a CLI and an MCP
server that converts a whole roll from one recipe, checks the result numerically, and lets
Claude Code grade frames with a colourist agent and a panel of judge agents. It drives
NegPy's own engine headless. It does not reimplement the conversion.

## Why

- One look across a roll. The recipe (`<roll>_recipe.json`) is the single source of truth;
  the review folder is always `batch(recipe)`.
- QA against your own images. Each frame is measured against your reference images and,
  if you have them, your approved finals.
- Less per-frame clicking. A roll starts from your base config plus a per-stock delta;
  problem frames get a small override through `negmcp fix`.
- AI colourist and judges. Claude Code subagents set the recipe and fix frames. Three
  judges rate the look. Templates are in `examples/claude/`.

## What to expect

Results are medium to good at best. negmcp does not replace grading by hand in NegPy.
Plan to finish the frames you care about in the app. To make that easy, `negmcp sidecars`
writes a `.negpy` file next to each scan, so NegPy opens the roll with the config negmcp
rendered.

## Install

Requirements: Python 3.13 or newer, git, [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/pylypenko-pavlo/negmcp negmcp && cd negmcp
bash vendor/setup.sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/negmcp --help
```

## Before you start: make a few reference frames

Convert a handful of frames by hand in NegPy until you like them. Export them as JPEG or
PNG into `<pos_root>/<roll>/`, keeping the scan's file name (`DSC00012.ARW` becomes
`DSC00012.jpg`). `negmcp qa` matches by file stem and reports, per frame and for the roll,
how far each axis sits from your approved version (the APPROVED section). These frames
are the target.

The `refs` folder works differently. It holds images whose look you like, from any source.
negmcp measures them once and derives a corridor (p10 to p90 per axis) plus
`house_look.json`. Refs give a general range of taste. Approved finals give the exact
target for one roll.

## Setup

```bash
negmcp init                  # starter look in ~/.local/share/negmcp/look
mkdir -p ~/negmcp/{neg,refs,pos}
negmcp start                 # config check, NegPy update + smoke render, refs, recipes, MCP import
```

Defaults, all overridable in `~/.config/negmcp/config.toml`, with `NEGMCP_<KEY>`, or with
`negmcp -c key=value`:

| Key | Default | Holds |
|---|---|---|
| `neg_root` | `~/negmcp/neg` | `<roll>/` folders with raw scans and `info.txt` |
| `refs_dir` | `~/negmcp/refs` | reference images |
| `pos_root` | `~/negmcp/pos` | `<roll>/` with approved finals |
| `review_root` | `~/negmcp/review` | rendered rolls, regenerated on every batch |
| `look_dir` | `~/.local/share/negmcp/look` | `base_template.json`, `film_deltas.json`, `house_look.json` |
| `rolls_dir` | `~/.local/share/negmcp/rolls` | `<roll>_recipe.json` |

`negmcp start` prints every path and where it came from. Full list: `docs/config.md`;
template: `negmcp.example.toml`.

## Per-roll flow

A roll is a folder `<neg_root>/<roll>/` with the scans and an `info.txt`:

```text
film: Kodak Gold 200
format: 35mm                      # or half-frame
negpy_crosstalk_profile: kodak_gold_200
```

```bash
negmcp new <roll>                 # recipe from base_template + the stock's delta
negmcp autocrop <roll> --apply    # 35mm: NegPy's roll autocrop into the recipe
negmcp batch <roll>               # iteration-size render into review_root/<roll>/
negmcp qa <roll>                  # numeric gate, refs corridor, approved finals
negmcp fix <roll> DSC00012 FF '{"density": 0.95}'   # per-frame override in the recipe
negmcp batch <roll> --final       # full resolution
negmcp sidecars <roll>            # .negpy next to each scan (35mm / full frame)
```

There is no `final` command. `batch --final` renders at full size and stamps the NegPy
version into the recipe. Repeat batch, qa and fix until the roll is clean. Details and the
reasoning behind each rule: `docs/workflow.md`.

## Claude Code

`negmcp serve` is a stdio MCP server. It exposes `render_frame`, `render_roll`,
`analyze_frame`, `fingerprint`, `write_sidecar` and `write_sidecars` (`docs/mcp-tools.md`).
It reads the same config as the CLI.

```bash
claude mcp add negmcp -- /abs/path/to/negmcp/.venv/bin/negmcp serve
```

Or put `examples/claude/mcp.json` into your project's `.mcp.json` and fix the path.

`examples/claude/agents/` has subagent templates: a colourist that sets the recipe and
fixes frames, three judges (technical, editorial, enthusiast) that rate the look, and a
pipeline engineer. Copy them into `.claude/agents/` and replace the placeholders with the
paths `negmcp start` prints. Your taste goes into the `taste` field of `house_look.json`,
which the agents read.

## Commands

`init`, `start`, `new`, `autocrop`, `batch`, `qa`, `fix`, `pool`, `sidecars`, `contact`,
`refs`, `corridor`, `replay`, `serve`. Run `negmcp <command> --help` for flags.
`replay` re-renders frames you graded in the NegPy app, reading its `edits.db` read-only.

## NegPy and the licence

negmcp is GPL-3.0-only (`LICENSE`). It imports NegPy at runtime and does not ship it.
`vendor/setup.sh` clones NegPy at the commit in `vendor/NEGPY_PIN` into `vendor/NegPy/`,
which git ignores. See `vendor/NOTICE.md`.

`negmcp start` moves NegPy to the newest release tag, smoke-renders the first scan, and
rolls back if that fails (`--no-update` skips this). A new engine version can change the
look. QA flags recipes graded on a different version (ENGINE) and shows how many frames
left the corridor. After an update, run `negmcp qa` on rolls you care about
(`docs/negpy-compat.md`).

## Limitations

- Scans are found by the `*.ARW` extension only. Other raw formats are not picked up.
- Tested on macOS. Linux is untested.
- Half-frame rolls get no sidecars: NegPy keeps one crop per scan file.
- The NegPy app reads a sidecar only for files it has no database entry for yet.
  `docs/workflow.md` explains how to reset that.
- QA reports symptoms and over-flags. A red car is not a cast. The final call is yours.

## Docs

- `docs/workflow.md`: the procedure, QA flags, levers
- `docs/config.md`: every config key and precedence
- `docs/mcp-tools.md`: MCP tool reference
- `docs/negpy-compat.md`: the pin and what to check after an engine update
- `examples/claude/`: MCP config and agent templates

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest
```

The tests need no scans. They render a synthetic DNG through the real NegPy engine.
