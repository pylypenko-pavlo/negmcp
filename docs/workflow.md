# Workflow

The same procedure for every roll and every film stock. What differs between stocks is the
NegPy crosstalk profile (and, once validated, a small per-stock delta) — not a new base per
roll. Re-tuning the whole base for each roll is the anti-pattern this flow exists to stop.

## Invariant: the recipe is the single source of truth

- `<rolls_dir>/<roll>_recipe.json` holds everything that makes a roll look the way it does:
  `base` (whole roll) + `per_frame_overrides` (`{stem: {...}}` for 35mm,
  `{stem: {"L": {...}, "R": {...}}}` for half-frame; a string = skip this frame, with the
  reason).
- The review folder (`<review_root>/<roll>/`) is **always** regenerated from the recipe:
  `batch(recipe) == review`. Never patch a review JPEG by hand — the next batch overwrites it.
- Writes go through `negmcp new / fix / autocrop --apply / pool --apply` (exclusive lock +
  atomic replace, so parallel fixers are safe). The recipe is validated against NegPy's own
  field list on every load: an unknown key is an error, notes are `_`-prefixed keys.
- `base.output_working_space` is required (`false` = colour-managed export in NegPy's export
  space, the honest default; `true` = the working-space buffer written as-is).
- Finished rolls can move to `<rolls_dir>/closed/`: still found by name, not checked by `start`.

## 0. Session start — `negmcp start`

Run before working on any roll. In order:

1. **config** — every path and where it came from (`cli` / `env` / `file` / `default`).
   Missing required paths fail the start; optional ones (refs, review, approved finals) are
   reported. No look yet → it tells you to run `negmcp init`.
2. **NegPy** — fetch tags; if a newer release exists, check it out, update
   `vendor/NEGPY_PIN`, install what the render path is missing, smoke-render the first scan
   under `neg_root`. On failure the checkout and the pin are rolled back and `start` exits
   non-zero. `--no-update` stays on the current pin.
3. **refs** — `refs_dir` is fingerprinted (names, sizes, mtimes). When it changed, it is
   measured once and both targets are rewritten: the QA corridor and `house_look.json`
   (`fingerprint` medians + `bands` p10..p90; your `taste` text is kept). `--recalibrate`
   forces it. "re-derived" means the target moved: re-check active rolls with `negmcp qa`.
4. **recipes** — every active recipe: valid? graded on which NegPy (`_graded_negpy_version`)?
5. **MCP** — `negmcp serve` imports.

Engine moves are not frozen out; they are caught. A recipe graded on NegPy X and rendered on
Y gets an ENGINE line in `negmcp qa`, and the corridor shows how many frames left it.

## 1. Roll context — `info.txt`

`<neg_root>/<roll>/info.txt`, one `key: value` per line:

```text
film: Kodak Gold 200
camera: <the camera the film was shot with>
lens: <lens>
format: 35mm                      # 35mm | full-frame | half-frame
push: +1                          # optional; "push: none" is not a push
process_mode: Transparency        # only for E-6 slides
negpy_crosstalk_profile: kodak_gold_200
developer: <lab or chemistry>     # optional, goes into the EXIF description
scanning: <scanner setup>         # optional
locations: <where>                # optional
```

- `negpy_crosstalk_profile` is a NegPy profile **file stem** (bundled ones:
  `vendor/NegPy/crosstalk/*.toml`; the app's user folder is searched too). Display names
  work, but stems are stable across NegPy versions.
- `camera` / `lens` become the JPEG's Make/Model/LensModel — the scan's own EXIF describes
  the digitising camera, not the shooting one.
- **Format decides the render path.** `35mm`: one image per scan. `half-frame`: each scan
  holds two portrait frames side by side; each half is rendered on its own crop (L/R), so the
  bright rebate and the gutter never enter the metering. Empty halves (end of film) are
  skipped automatically.

## 2. Recipe — `negmcp new <roll>`

Copies `base` from `<look_dir>/base_template.json`, sets `crosstalk_profile` from info.txt,
applies the matching entry of `film_deltas.json` (+ `_push` when pushed, `_transparency`
for slides) and records what was applied in `_stock_deltas_applied`. It never invents
per-roll tuning and refuses to overwrite an existing recipe. Half-frame rolls also get
NegPy's auto-split per scan as a `_half_frame_detect` note (informational).

**Your universal base.** `negmcp init` ships NegPy's defaults. Tune the base once, on a few
representative frames of a few rolls, against your refs — then stop. Per-stock corrections go
into `film_deltas.json` once validated on a real roll; per-frame problems go through
`negmcp fix`. Record why you changed the base (a changelog next to your look works well).

**Crops.** 35mm: `negmcp autocrop <roll> --apply` (NegPy's roll autocrop: one rect + fine
rotation per frame, frames that already have a crop are preserved; `--force` re-detects);
fix unresolved frames by hand with `negmcp fix`. Half-frame: the batch uses a per-side
default crop (`crop.DEFAULT_CROP`); the holder gate can drift between frames, so nudge
`manual_crop_rect` x0/x1 by ~0.01 per frame where needed. `negmcp autocrop` on half-frame
is a dry run (see `docs/negpy-compat.md`).

### Levers (NegPy flat keys — direction)

| Key | Effect |
|---|---|
| `density` | overall print density: **< 1 = brighter** |
| `white_point_offset` | highlight brightness (watch for clipping) |
| `black_point_offset` | shadow floor: negative = deeper blacks, positive = open shadows |
| `toe` / `shoulder` | shadow / highlight roll-off (a lot of toe = flat) |
| `grade` | contrast as ISO R; 115 = neutral, lower = harder; a value ≤ 5 is a legacy paper grade (`150 − 20·g`) |
| `luma_range_clip` | percentile clip of the luma bounds: more = deeper blacks and more saturation; high values blow colour |
| `color_separation` | crosstalk unmix with the roll's profile (`crosstalk_strength = value − 1`); 1.0 = off |
| `saturation` | Lab-stage saturation (1.0 = off) — the direct saturation lever |
| `dye_separation` | density-space saturation (1.0 = off) |
| `wb_yellow` / `wb_magenta` / `wb_cyan` | warm↔cool / green↔magenta (+ = de-green) / red↔cyan |
| `shadow_yellow` etc. | colour in one tonal zone only |
| `midtone_gamma`, `shadow_grade` / `highlight_grade`, `*_trim_red/green/blue` | finer tone controls; try one at a time |

Keep `auto_exposure` and `auto_normalize_contrast` on: every frame is metered on its own.
Judge colour on the iteration-size render and confirm on the final.

## 3. Batch — `negmcp batch <roll>`

```text
negmcp batch <roll> [--recipe P] [--format half|ff] [--final] [--long-edge N] [--only A,B] [--out DIR]
```

- Iteration size by default: the **cropped output's** long edge = `iter_long_edge` (2200);
  the source is downscaled before the tone maths (~2× faster, fingerprint within ~0.01 of
  full resolution). Full resolution only with `--final`.
- Parallel (`workers`, default cpu − 2); one broken scan is reported, it does not stop the
  roll. EXIF comes from info.txt; the scan date from the raw.
- Every run writes `_manifest.json` (recipe hash, NegPy version and pin, the recipe's graded
  version, output mode, size, frames) and, for a whole roll, a contact sheet in `work_dir`.

## 4. QA — `negmcp qa <roll>`

```text
negmcp qa <roll|dir> [--json F] [--contact F] [--long N] [--approved DIR | --no-approved]
```

Measures every JPEG at ~1500 px. Exit 1 on any HARD flag, 2 if the corridor step could not
run.

- **HARD** (blocks): green cast, grey-green shadows, milky lift, clipped highlights, muddy
  underexposure; regionally: rebate in frame, green skin, washed/blown sky.
- **REVIEW**: probably a warm scene — look at it.
- **CORRIDOR** (always): each frame in sRGB against the refs corridor (span floor; key / gm
  / yb bands = refs p10..p90; sat for information). Outside the corridor is REVIEW, not
  HARD: by construction ~20 % of the refs themselves sit outside each band.
- **ENGINE**: the recipe was graded on another NegPy than the one rendering now.
- **APPROVED** (when `<pos_root>/<roll>/` has approved finals): signed delta per axis against
  them, per frame and for the roll; report only.
- Info: NegPy's own meters per frame, `negmcp pool` outliers, half-frame split failures.

QA reports **symptoms**; it is biased to over-flag. Scene colour is not a defect (a red car,
a sunset, warm interior light, foliage); a cast on neutrals, skin, sky or foliage is. The
final word is a human's (or a tribunal of judge agents' — see `examples/claude/agents/`).
For tooling, read `--json`: `<stem>.corridor.out`, `_pool_outliers`,
`_half_frame_detect_failed`.

**Roll pooling** (`negmcp pool <roll> [--apply]`) — shared colour/cast bounds across the roll,
luma stays per frame. Off by default: on rolls with mixed scenes it did not reduce the
frame-to-frame white-balance spread. A locked roll-median baseline (bound-lock) is worse
still on such rolls; prefer per-frame fixes.

## 5. Fixes — `negmcp fix`

```text
negmcp fix <roll|recipe> <STEM> <L|R|FF> '<json of flat NegPy keys>'
```

Merges into the frame's override (earlier fixes survive), validates, writes atomically.
Frames are independent once the base is set, so fixes can run in parallel. Re-render the
fixed frames (`negmcp batch <roll> --only A,B`) and re-run `negmcp qa`.

One colourist sets the roll's base (one hand = one coherent look); fixes can be spread over
many workers.

## 6. Final

```text
negmcp batch <roll> --final        # full resolution; clears old JPEGs; stamps _graded_negpy_version
negmcp qa <roll>                   # once more on the finals
negmcp sidecars <roll>             # .negpy next to each scan (full-frame rolls)
negmcp contact <roll>              # film-strip contact sheet
```

`--only` never stamps the version. Half-frame rolls get no sidecars: NegPy keeps one sidecar
(one crop) per scan, which cannot hold two L/R crops.

**Opening the roll in the NegPy app.** The app reads a `.negpy` sidecar only when it has no
entry for that file yet (keyed by a content hash, so renaming does not help). To make it
pick up new sidecars, close the app, back up its `edits.db`, remove the files' rows from it
(or start from an empty database), and reopen the folder. negmcp itself only ever reads
`edits.db` (`negmcp replay`).

## Refs and `house_look.json`

`refs_dir` holds images whose look you want (JPEG/PNG, any embedded ICC — converted to sRGB
before measuring). negmcp measures them with the same fingerprint QA and the MCP tools print
(black / white / span / key / mean / sat / green-magenta / yellow-blue / mid-tone WB) and
writes:

- `<work_dir>/corridor/corridor.json` — the bands `negmcp qa` checks against;
- `house_look.json` — medians + bands, what the MCP `fingerprint` tool compares a frame to;
- `<work_dir>/refs_contact.jpg` — the refs at a glance.

`house_look.json` → `taste` is free text in your words; negmcp never parses it, the
colourist and judge agents read it.

## Replay of app-graded rolls

`negmcp replay <roll>` re-renders each scan with the config the NegPy app stored for it in
`edits.db` (matched by NegPy's file hash). Frames without a match are listed and can be
rendered from a fallback recipe; `<roll>_overrides.json` layers one-off fixes on top.
