"""``negmcp`` command line.

Global options apply to every subcommand and override env/config-file values:
  --config FILE        config file instead of ~/.config/negmcp/config.toml
  -c / --set KEY=VAL   any config key (negmcp/config.py KEYS), repeatable
  --log-level LEVEL

Subcommands are registered in ``COMMANDS`` (name -> (help, add_arguments, handler));
adding one is a new entry there.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from negmcp.config import ENV_PREFIX, KEYS, Config, get_config

__all__ = ["main"]

log = logging.getLogger("negmcp")

Handler = Callable[[Config, argparse.Namespace], int]


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
def _add_start(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-update", action="store_true", help="offline: do not fetch / move NegPy to a newer release")
    p.add_argument(
        "--recalibrate",
        action="store_true",
        help="re-derive the refs corridor + house_look.json even if the refs did not change",
    )
    p.add_argument("--refs", type=Path, help="save this refs_dir into the config file, then start with it")


def _cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.config import save_config_value
    from negmcp.startup import run_start

    if args.refs:
        refs = args.refs.expanduser().resolve()
        if not refs.is_dir():
            raise ValueError(f"--refs {refs}: not a directory")
        saved = save_config_value("refs_dir", str(refs))
        print(f"refs_dir = {refs} saved to {saved}")
        if os.environ.get(f"{ENV_PREFIX}REFS_DIR"):
            print(f"  note: env {ENV_PREFIX}REFS_DIR / -c refs_dir still overrides the file", file=sys.stderr)
        get_config.cache_clear()
        cfg = get_config()
    rep = run_start(cfg, update=not args.no_update, recalibrate=args.recalibrate, cli_keys=frozenset(CLI_KEYS))
    print("\n".join(rep.lines))
    return 1 if rep.failed else 0


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
def _add_init(p: argparse.ArgumentParser) -> None:
    p.add_argument("--look-dir", type=Path, help="where to create the look (saved into the config file)")
    p.add_argument("--rolls-dir", type=Path, help="where recipes live (saved into the config file)")


def _cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.config import save_config_value
    from negmcp.look import init_look

    for key in ("look_dir", "rolls_dir"):
        value = getattr(args, key)
        if value is None:
            continue
        path = value.expanduser().resolve()
        saved = save_config_value(key, str(path))
        print(f"{key} = {path} saved to {saved}")
        if os.environ.get(f"{ENV_PREFIX}{key.upper()}") and key not in CLI_KEYS:
            print(f"  note: env {ENV_PREFIX}{key.upper()} still overrides the file", file=sys.stderr)
    get_config.cache_clear()
    cfg = get_config()
    for path, written in init_look(cfg.look_dir, cfg.rolls_dir):
        print(f"  {'created' if written else 'kept   '} {path}")
    print(f"  rolls_dir {cfg.rolls_dir}")
    print(
        "Next: write your taste into house_look.json, put reference images into refs_dir, "
        "then `negmcp start` (it derives the QA corridor + house_look target from the refs)."
    )
    return 0


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------
def _add_new(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (neg_root/<roll>) or path to the roll directory")
    p.add_argument(
        "--autocrop",
        action="store_true",
        help="35mm: write NegPy's roll autocrop into the new recipe (= `negmcp autocrop <roll> --apply`)",
    )


def _cmd_new(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.exif import RollFormat, parse_info_txt, roll_format
    from negmcp.recipe import new_recipe

    roll_dir = cfg.roll_dir(args.roll)
    path, recipe = new_recipe(roll_dir, cfg.rolls_dir, cfg.look_dir)
    applied = recipe["_stock_deltas_applied"]
    tag = f" + deltas {applied['deltas']}" if applied["deltas"] else " (no stock deltas — base fits as-is)"
    print(f"{path}  <-  base_template + crosstalk={applied['profile']}{tag}")
    if not applied["known_stock"]:
        print(
            f"  NOTE: '{applied['profile']}' not in film_deltas.json — universal base only. "
            "Validate its look and add its deltas to film_deltas.json when known.",
            file=sys.stderr,
        )
    detect = recipe.get("_half_frame_detect")
    if detect:
        n, failed = len(detect["frames"]), detect["failed"]
        print(f"half-frame auto-split: {n - len(failed)}/{n} clean (recorded; halves crop with DEFAULT_CROP)")
        if failed:
            print(f"  WARNING: detector failed/partial on {len(failed)}: {failed}", file=sys.stderr)
    fmt = roll_format(parse_info_txt(roll_dir / "info.txt"))
    if args.autocrop and fmt is RollFormat.HALF:
        print(
            "  --autocrop: not applied on half-frame (NegPy's per-half crop keeps rebate where DEFAULT_CROP "
            "does not, docs/negpy-compat.md); dry run: negmcp autocrop " + roll_dir.name,
            file=sys.stderr,
        )
    elif args.autocrop and fmt is None:
        print(
            "  --autocrop: info.txt has no `format:` line; run `negmcp autocrop <roll> --format ff --apply`",
            file=sys.stderr,
        )
        return 2
    elif args.autocrop:
        return _run_autocrop(cfg, roll_dir, path, fmt, apply=True, force=False)
    print("Next: `negmcp autocrop <roll> --apply` on 35mm (NegPy roll autocrop), then batch/QA/tribunal.")
    return 0


# ---------------------------------------------------------------------------
# autocrop
# ---------------------------------------------------------------------------
def _add_autocrop(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (neg_root/<roll>) or path to the roll directory")
    p.add_argument("--recipe", type=Path, help="recipe path (default rolls_dir/<roll>_recipe.json)")
    p.add_argument("--format", choices=["half", "ff"], help="override info.txt `format:`")
    p.add_argument("--apply", action="store_true", help="write manual_crop_rect (+ fine_rotation) into the recipe")
    p.add_argument("--force", action="store_true", help="re-detect frames whose override already has a crop")


def _run_autocrop(cfg: Config, roll_dir: Path, recipe_path: Path, fmt, *, apply: bool, force: bool) -> int:
    from negmcp.autocrop import autocrop_roll, recipe_fixes
    from negmcp.exif import RollFormat
    from negmcp.recipe import apply_fixes, load_recipe

    res = autocrop_roll(roll_dir, load_recipe(recipe_path, fmt=fmt), fmt, force=force, workers=cfg.workers)
    for f in res.frames:
        rot = f"  fine_rotation {f.fine_rotation:+.3f}" if f.fine_rotation is not None else ""
        tag = "roll-calibrated" if f.calibrated else "own detection"
        print(f"{f.name:32s} {list(f.rect)}  conf {f.confidence:.2f} ({tag}){rot}")
    total = len(res.frames) + len(res.unresolved) + len(res.errors)
    print(f"RESOLVED {len(res.frames)}/{total} | preserved {len(res.preserved)} | recipe skips {len(res.skipped)}")
    if res.unresolved:
        keep = "DEFAULT_CROP" if fmt is RollFormat.HALF else "no crop (rebate stays in!)"
        print(f"  UNRESOLVED {len(res.unresolved)} -> {keep}: {res.unresolved}", file=sys.stderr)
    if res.errors:
        print(f"  ERRORS {len(res.errors)}: {res.errors}", file=sys.stderr)
    if res.preserved:
        print(f"  preserved (override already has a crop; --force re-detects): {res.preserved}")
    if not apply:
        print("dry run: --apply writes them into the recipe")
        return 1 if res.errors else 0
    if fmt is RollFormat.HALF:
        print(
            "  WARNING: half-frame: NegPy's per-half crop can keep rebate slivers (docs/negpy-compat.md)",
            file=sys.stderr,
        )
    apply_fixes(recipe_path, recipe_fixes(res))
    print(f"applied {len(res.frames)} -> {recipe_path}")
    return 1 if res.errors else 0


def _cmd_autocrop(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.batch import resolve_format

    roll_dir = cfg.roll_dir(args.roll)
    fmt = resolve_format(roll_dir, args.format)
    recipe_path = args.recipe or cfg.recipe_path(roll_dir.name)
    return _run_autocrop(cfg, roll_dir, recipe_path, fmt, apply=args.apply, force=args.force)


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------
def _add_batch(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (neg_root/<roll>) or path to the roll directory")
    p.add_argument("--recipe", type=Path, help="recipe path (default rolls_dir/<roll>_recipe.json)")
    p.add_argument("--format", choices=["half", "ff"], help="override info.txt `format:` (half-frame L/R | full frame)")
    p.add_argument("--final", action="store_true", help="full resolution (default: iter_long_edge)")
    p.add_argument("--long-edge", type=int, help="iteration long edge of the OUTPUT frame (default iter_long_edge)")
    p.add_argument("--only", help="comma-separated ARW stems to render (subset)")
    p.add_argument("--out", type=Path, help="output dir (default review_root/<roll>)")


def _cmd_batch(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.batch import run_batch

    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    res = run_batch(
        cfg,
        args.roll,
        recipe_path=args.recipe,
        fmt=args.format,
        final=args.final,
        long_edge=args.long_edge,
        only=only,
        out_dir=args.out,
    )
    out = res.manifest.parent if res.manifest else args.out
    print(f"RENDERED {len(res.rendered)} -> {out} | SKIPPED {len(res.skipped)}: {res.skipped} | final={args.final}")
    if res.errors:
        print(f"ERRORS {len(res.errors)}: {res.errors}", file=sys.stderr)
    print(f"manifest -> {res.manifest}")
    if res.engine_warning:
        print(f"WARNING: {res.engine_warning}", file=sys.stderr)
    if res.stamped:
        print(f"recipe stamped _graded_negpy_version={res.stamped}")
    if res.contact:
        print(f"contact -> {res.contact}")
    return 0 if res.rendered and not res.errors else 1


# ---------------------------------------------------------------------------
# qa
# ---------------------------------------------------------------------------
def _add_qa(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="roll name (-> review_root/<roll>) or a directory of JPGs")
    p.add_argument("--json", type=Path, help="write per-frame flags + metrics here")
    p.add_argument("--contact", type=Path, help="write a contact sheet of HARD-flagged frames here")
    p.add_argument("--long", type=int, default=1500, help="analysis long edge (default 1500)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--approved", type=Path, help="approved finals of the roll (default pos_root/<roll>)")
    g.add_argument("--no-approved", action="store_true", help="skip the comparison with approved finals")


def _cmd_qa(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.qa import run

    target = Path(args.target).expanduser()
    d = target if target.is_dir() else cfg.review_dir(args.target)
    approved = args.approved.expanduser() if args.approved else None
    return run(d, args.long, args.json, args.contact, approved, not args.no_approved)


# ---------------------------------------------------------------------------
# pool
# ---------------------------------------------------------------------------
def _add_pool(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (neg_root/<roll>) or path to the roll directory")
    p.add_argument("--recipe", type=Path, help="recipe path (default rolls_dir/<roll>_recipe.json)")
    p.add_argument("--format", choices=["half", "ff"], help="override info.txt `format:`")
    p.add_argument("--apply", action="store_true", help="write the pooled colour/cast baseline into the recipe")


def _cmd_pool(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.batch import resolve_format
    from negmcp.pool import apply_pool, pool_cache_path, pool_roll

    roll_dir = cfg.roll_dir(args.roll)
    recipe_path = args.recipe or cfg.recipe_path(roll_dir.name)
    res = pool_roll(cfg, args.roll, recipe_path=recipe_path, fmt=args.format)
    upd = res.base_update()
    axis = "none (no confident neutrals)" if res.axis is None else f"mid {[round(v, 3) for v in res.axis[0]]}"
    print(f"POOLED {len(res.frames)} frames | outliers {len(res.outliers)}: {res.outliers}")
    print(f"  floors {[round(v, 4) for v in res.floors]}  ceils {[round(v, 4) for v in res.ceils]}  axis {axis}")
    if res.errors:
        print(f"  errors {len(res.errors)}: {res.errors}", file=sys.stderr)
    print(f"  cache -> {pool_cache_path(cfg, roll_dir.name)}")
    if args.apply:
        apply_pool(recipe_path, res, resolve_format(roll_dir, args.format))
        print(f"applied -> {recipe_path}: base {sorted(upd)}; outliers keep their own colour/cast")
    else:
        print("dry run: --apply writes it into the recipe (colour + cast ride the pool, luma stays per frame)")
    return 0


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------
def _add_fix(p: argparse.ArgumentParser) -> None:
    p.add_argument("recipe", help="recipe path, or roll name (-> rolls_dir/<roll>_recipe.json)")
    p.add_argument("stem", help="ARW stem, e.g. DSC00001")
    p.add_argument("side", choices=["L", "R", "FF"], help="half-frame side, or FF for a full-frame roll")
    p.add_argument("overrides", help="JSON object of flat NegPy keys to MERGE into the frame's override")


def _cmd_fix(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.recipe import apply_fix

    overrides = json.loads(args.overrides)
    if not isinstance(overrides, dict):
        raise SystemExit("overrides must be a JSON object")
    merged = apply_fix(cfg.recipe_path(args.recipe), args.stem, args.side, overrides)
    label = args.stem if args.side == "FF" else f"{args.stem}_{args.side}"
    print(f"{label} -> recipe (merged): {merged}")
    return 0


# ---------------------------------------------------------------------------
# sidecars
# ---------------------------------------------------------------------------
def _add_sidecars(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (recipe rolls_dir/<roll>_recipe.json, ARWs neg_root/<roll>)")
    p.add_argument("--recipe", type=Path, help="recipe path override")
    p.add_argument("--format", choices=["half", "ff"], help="override info.txt `format:`")


def _cmd_sidecars(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.exif import RollFormat
    from negmcp.recipe import SidecarRefusedError, write_sidecars

    roll_dir = cfg.roll_dir(args.roll)
    try:
        written, skipped = write_sidecars(
            args.recipe or cfg.recipe_path(roll_dir.name), roll_dir, RollFormat(args.format) if args.format else None
        )
    except SidecarRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"SIDECARS {len(written)} -> {roll_dir} | skipped by recipe: {skipped}")
    return 0


# ---------------------------------------------------------------------------
# contact
# ---------------------------------------------------------------------------
def _add_contact(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="roll name (-> review_root/<roll>) or a directory of finals")
    p.add_argument("--film", help="edge-print text (default: info.txt `film:`)")
    p.add_argument("--out", type=Path, help="output JPG (default review_root/<roll>_contact.jpg)")
    p.add_argument("--per-row", type=int, default=5)


def _cmd_contact(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.contact import build
    from negmcp.exif import load_roll_info

    target = Path(args.target).expanduser()
    review = target if target.is_dir() else cfg.review_dir(args.target)
    film = args.film or load_roll_info(review.name).get("film", "FILM")
    build(review, args.out or review.parent / f"{review.name}_contact.jpg", film, args.per_row)
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def _add_serve(p: argparse.ArgumentParser) -> None:
    del p


def _cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.server import serve

    serve()
    return 0


# ---------------------------------------------------------------------------
# corridor
# ---------------------------------------------------------------------------
def _add_corridor(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["calibrate", "run"])
    p.add_argument("roll", nargs="?", help="roll for `run` (neg_root/<roll> with info.txt)")


def _cmd_corridor(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp import corridor

    corridor.main([args.action, *([args.roll] if args.roll else [])])
    return 0


# ---------------------------------------------------------------------------
# refs
# ---------------------------------------------------------------------------
def _add_refs(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=["check", "derive"],
        help="check: re-derive what the refs set made stale (default); derive: force both",
    )


def _cmd_refs(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp import refs

    st = refs.ensure(force=args.action == "derive")
    print(f"refs    {refs.summary(st)}")
    if st.house is not None:
        fp, bands = st.house["fingerprint"], st.house["bands"]
        print(f"  house_look {cfg.house_look} (median [p10..p90] over {st.house['n_refs']} refs, sRGB)")
        print(
            "  "
            + "  ".join(
                f"{k}={fp[k]:.3f}" + (f"[{bands[k][0]:.3f}..{bands[k][1]:.3f}]" if k in bands else "")
                for k in refs.HOUSE_SCALARS
            )
        )
        print(f"  mid_wb={fp['mid_wb']}  shadow_wb={fp['shadow_wb']}")
        kept = [k for k in st.house if k not in refs.DERIVED_KEYS]
        print(f"  kept as written: {kept}")
    if st.contact:
        print(f"  contact -> {st.contact}")
    if st.corridor_why or st.house_why:
        print("  refs-derived targets moved: re-check active rolls (`negmcp qa <roll>`)")
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def _add_replay(p: argparse.ArgumentParser) -> None:
    p.add_argument("roll", help="roll name (neg_root/<roll>) or path to the roll directory")
    p.add_argument("--fallback-recipe", type=Path, help="recipe whose base renders frames missing from edits.db")
    p.add_argument("--overrides", type=Path, help="{stem: {field: value}} layered on the edits.db config")
    p.add_argument("--out", type=Path, help="output dir (default review_root/<roll>)")
    p.add_argument("--long-edge", type=int, help="long edge of the OUTPUT frame (default: full resolution)")
    p.add_argument(
        "--working-space",
        action="store_true",
        help="write working-space values tagged as the target (parity with pre-0.53 exports)",
    )


def _cmd_replay(cfg: Config, args: argparse.Namespace) -> int:
    from negmcp.replay import run_replay

    res = run_replay(
        cfg,
        args.roll,
        fallback_recipe=args.fallback_recipe,
        overrides_path=args.overrides,
        out_dir=args.out,
        long_edge=args.long_edge,
        working_space=args.working_space,
    )
    if res.overrides_for:
        print(f"OVERRIDES applied for: {res.overrides_for}")
    print(
        f"REPLAYED {len(res.replayed)}/{res.total} | FALLBACK {len(res.fallback)} | SKIPPED {len(res.skipped)}: {res.skipped}"
    )
    if res.unmatched:
        print(f"NOT IN edits.db (hash miss): {res.unmatched}")
    return 0 if (res.replayed or res.fallback) else 1


COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Handler]] = {
    "start": (
        "session start: config, NegPy auto-update + smoke, refs (corridor + house_look), recipes, MCP",
        _add_start,
        _cmd_start,
    ),
    "init": ("create a starter look (base_template, film_deltas, house_look) in look_dir", _add_init, _cmd_init),
    "new": ("birth <roll>_recipe.json from base_template + info.txt", _add_new, _cmd_new),
    "autocrop": (
        "NegPy's roll autocrop -> per-frame manual_crop_rect (dry run / --apply)",
        _add_autocrop,
        _cmd_autocrop,
    ),
    "batch": ("render a roll from its recipe into the review folder", _add_batch, _cmd_batch),
    "qa": ("numeric QA gate over a folder of rendered JPGs (exit 1 on HARD)", _add_qa, _cmd_qa),
    "pool": ("pool colour/cast bounds across the roll (NegPy roll analysis)", _add_pool, _cmd_pool),
    "fix": ("merge a per-frame override into the recipe (the SoT write path)", _add_fix, _cmd_fix),
    "sidecars": ("write .negpy sidecars of a full-frame roll from its recipe", _add_sidecars, _cmd_sidecars),
    "contact": ("film-strip contact sheet of a roll's finals", _add_contact, _cmd_contact),
    "serve": ("run the MCP server (stdio)", _add_serve, _cmd_serve),
    "corridor": ("corridor engine: calibrate on refs / run on rolls", _add_corridor, _cmd_corridor),
    "refs": ("measure refs_dir once -> QA corridor + house_look.json (check / derive)", _add_refs, _cmd_refs),
    "replay": ("replay the NegPy app's edits.db config per frame", _add_replay, _cmd_replay),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="negmcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, help="config file (default ~/.config/negmcp/config.toml)")
    parser.add_argument("-c", "--set", action="append", default=[], metavar="KEY=VALUE", help="override a config key")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (help_text, add_args, handler) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        add_args(p)
        p.set_defaults(func=handler)
    return parser


CLI_KEYS: set[str] = set()  # config keys set by -c/--set/--log-level (for `start`'s provenance)


def _export_overrides(args: argparse.Namespace) -> None:
    """CLI > env: export flags as NEGMCP_* so spawn workers resolve the same config."""
    if args.config:
        os.environ[f"{ENV_PREFIX}CONFIG"] = str(args.config.expanduser())
    pairs = list(args.set) + ([f"log_level={args.log_level}"] if args.log_level else [])
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in KEYS:
            raise SystemExit(f"--set expects KEY=VALUE with KEY in {sorted(KEYS)} (got {pair!r})")
        os.environ[f"{ENV_PREFIX}{key.upper()}"] = value
        CLI_KEYS.add(key)
    get_config.cache_clear()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _export_overrides(args)
    cfg = get_config()
    logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return args.func(cfg, args)
    except (ValueError, OSError, RuntimeError) as exc:  # expected user-facing failures: no traceback
        if cfg.log_level == "DEBUG":
            raise
        print(f"negmcp {args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
