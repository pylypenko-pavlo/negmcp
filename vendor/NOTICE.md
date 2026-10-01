# vendor/ — external reference, NOT our code

`NegPy/` is a **pinned git clone of upstream** NegPy
(github.com/marcinz606/NegPy) at the commit/version in **`vendor/NEGPY_PIN`**
(detached HEAD). It is a *reference*, not a copy we maintain — it carries its own
`.git`, and we never edit anything inside it.

- **License:** NegPy is **GPL-3.0** (copyleft). This tree is upstream's, unmodified.
- **Import root:** `negmcp/_negpy.py` puts the config's `negpy_src` (default
  `<repo>/vendor/NegPy`) on `sys.path` and refuses to run if `VERSION` / `git HEAD`
  differ from `NEGPY_PIN`.
- **Reproduce:** `bash vendor/setup.sh` (clones + checks out the pin from `NEGPY_PIN`).
- **Update:** `negmcp start` fetches tags, moves the checkout + `NEGPY_PIN` to the newest
  release tag, smoke-renders and rolls both back on failure (`--no-update` skips it). By hand:
  edit `NEGPY_PIN`, run `bash vendor/setup.sh`. Either way, check render parity
  (`docs/negpy-compat.md`) and refresh your local render baseline
  (`tests/fixtures/make_baseline.py`).
- **Not committed to our repo:** `vendor/NegPy` is git-ignored.
