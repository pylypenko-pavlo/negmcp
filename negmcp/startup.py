"""``negmcp start`` — the session start check, in order:

1. config: the resolved paths and where each comes from (cli/env/file/default);
2. NegPy: fetch tags, move to the latest release tag if newer than the pin, install what the
   render tract is missing, smoke-render one small frame; on failure roll the checkout and the
   pin back and exit non-zero;
3. refs: count + metadata hash; when the set changed, measure it once and rewrite both
   refs-derived targets, the QA corridor and house_look.json (``negmcp.refs``);
4. recipes in rolls_dir (``closed/`` is not checked): valid?, graded on which NegPy;
5. ``negmcp serve`` imports.

State (last refs hash, NegPy version) lives in ``<cache_dir>/state.json``. The engine may move
under us, so the update and smoke touch NegPy only in subprocesses; ``negmcp.render`` is first
imported in-process by recipe validation (step 4), i.e. after the pin is final.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from negmcp import _negpy
from negmcp.config import CLOSED_SUBDIR, Config, config_file_path, config_sources

__all__ = ["StartReport", "latest_release_tag", "run_start", "update_negpy"]

log = logging.getLogger(__name__)
SHOWN_PATHS = ("refs_dir", "neg_root", "review_root", "pos_root", "look_dir", "rolls_dir", "negpy_src")
# Paths a fresh setup may lack without failing `start` (why it is fine).
OPTIONAL_PATHS = {
    "refs_dir": "no QA corridor / house_look target until it exists",
    "review_root": "created by `negmcp batch`",
    "pos_root": "approved finals are optional",
    "rolls_dir": "created by `negmcp init` / `negmcp new`",
}
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")  # releases only: no -rc / -beta / .dev tags
SMOKE_LONG_EDGE = 400
SMOKE_TIMEOUT_S = 300
FETCH_TIMEOUT_S = 60
MAX_DEP_INSTALLS = 5
# import name -> distribution name, where they differ (everything else: same name, _ -> -)
_MODULE_DIST = {"cv2": "opencv-python-headless", "PIL": "pillow", "yaml": "pyyaml", "skimage": "scikit-image"}

SmokeFn = Callable[[Path | None], tuple[bool, str]]


@dataclass(slots=True)
class StartReport:
    lines: list[str] = field(default_factory=list)
    failed: bool = False

    def add(self, line: str) -> None:
        self.lines.append(line)


# ---------------------------------------------------------------------------
# git / pin
# ---------------------------------------------------------------------------
def _git(src: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True, timeout=timeout, check=False)


def latest_release_tag(src: Path) -> tuple[str, tuple[int, int, int]] | None:
    """Highest ``X.Y.Z`` tag (pre-releases excluded), as (tag, version tuple)."""
    best: tuple[str, tuple[int, int, int]] | None = None
    for tag in _git(src, "tag", "--list").stdout.split():
        m = SEMVER.match(tag)
        if m:
            ver = (int(m[1]), int(m[2]), int(m[3]))
            if best is None or ver > best[1]:
                best = (tag, ver)
    return best


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(version)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _head(src: Path) -> str:
    return _git(src, "rev-parse", "--short=7", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# smoke render (subprocess) + missing-dependency install
# ---------------------------------------------------------------------------
_SMOKE_CODE = """
import sys, time
t = time.perf_counter()
from negmcp import render
arw = sys.argv[1] if len(sys.argv) > 1 else ""
if arw:
    im = render.render(arw, {"process_mode": "C41", "output_working_space": False}, long_edge=int(sys.argv[2]))
    print(f"render ok {im.size[0]}x{im.size[1]} in {time.perf_counter() - t:.1f}s")
else:
    print("import ok (no ARW under neg_root to render)")
"""


def _smoke_subprocess(arw: Path | None) -> tuple[bool, str]:
    args = [sys.executable, "-c", _SMOKE_CODE, *([str(arw), str(SMOKE_LONG_EDGE)] if arw else [])]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return False, f"smoke render timed out after {SMOKE_TIMEOUT_S}s"
    if res.returncode == 0:
        return True, res.stdout.strip().splitlines()[-1]
    tail = (res.stderr or res.stdout).strip().splitlines()
    return False, tail[-1] if tail else f"exit {res.returncode}"


def _vendor_requirements(src: Path) -> dict[str, str]:
    """normalised dist name -> requirement string, from vendor/NegPy/pyproject.toml."""
    with (src / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh).get("project", {}).get("dependencies", [])
    out = {}
    for req in deps:
        name = re.split(r"[\s<>=!~;\[]", req, maxsplit=1)[0]
        out[re.sub(r"[-_.]+", "-", name).lower()] = req
    return out


def _install_missing(module: str, src: Path) -> str:
    """uv-install upstream's pinned requirement for a module the render tract failed to import."""
    dist = _MODULE_DIST.get(module, module.replace("_", "-")).lower()
    req = _vendor_requirements(src).get(dist)
    if req is None:
        raise RuntimeError(f"render tract needs module {module!r}, which NegPy's pyproject does not declare")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError(f"missing dependency {req}: uv not on PATH (install it, or pip install {req!r})")
    res = subprocess.run(
        [uv, "pip", "install", "--python", sys.executable, req], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        raise RuntimeError(f"uv pip install {req} failed: {res.stderr.strip().splitlines()[-1:]}")
    return req


def smoke_with_deps(arw: Path | None, src: Path, smoke: SmokeFn) -> tuple[bool, str, list[str]]:
    """Smoke-render; on ``No module named X`` install X's upstream pin and retry."""
    installed: list[str] = []
    for _ in range(MAX_DEP_INSTALLS + 1):
        ok, msg = smoke(arw)
        m = re.search(r"No module named '([\w.]+)'", msg)
        if ok or not m:
            return ok, msg, installed
        try:
            installed.append(_install_missing(m[1].split(".")[0], src))
        except RuntimeError as exc:
            return False, str(exc), installed
    return False, f"still failing after installing {installed}: {msg}", installed


def _smoke_arw(cfg: Config) -> Path | None:
    """First ARW of the first roll under neg_root (deterministic, any roll will do)."""
    if not cfg.neg_root.is_dir():
        return None
    for roll in sorted(p for p in cfg.neg_root.iterdir() if p.is_dir()):
        arws = sorted(p for p in roll.iterdir() if p.suffix.lower() == ".arw")
        if arws:
            return arws[0]
    return None


# ---------------------------------------------------------------------------
# NegPy update
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class UpdateResult:
    ok: bool
    line: str
    old: str
    new: str


def update_negpy(
    src: Path,
    pin_file: Path,
    arw: Path | None,
    *,
    fetch: bool = True,
    smoke: SmokeFn = _smoke_subprocess,
) -> UpdateResult:
    """Move vendor/NegPy to the latest release tag when it is newer than the pin; smoke it;
    roll back checkout + pin on failure. Without a newer tag, only smoke the current pin."""
    pin = _negpy.read_pin(pin_file)
    notes: list[str] = []
    if fetch:
        try:
            res = _git(src, "fetch", "--tags", "--quiet", "origin", timeout=FETCH_TIMEOUT_S)
            if res.returncode != 0:
                notes.append("fetch failed (offline?), using local tags")
        except subprocess.TimeoutExpired:
            notes.append("fetch timed out (offline?), using local tags")
    latest = latest_release_tag(src) if fetch else None
    current = _version_tuple(pin.version)
    head = _head(src)
    if not head.startswith(pin.commit[:7]):
        return UpdateResult(False, f"checkout is at {head}, pin is {pin.commit}: run vendor/setup.sh", pin.version, "")

    if latest is None or current is None or latest[1] <= current:
        ok, msg, deps = smoke_with_deps(arw, src, smoke)
        status = "up to date" if fetch else "update skipped (--no-update)"
        line = f"{pin.version} ({pin.commit}) {status}; smoke {msg}" + (f"; installed {deps}" if deps else "")
        return UpdateResult(ok, "; ".join([line, *notes]), pin.version, pin.version)

    tag = latest[0]
    if _git(src, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return UpdateResult(False, f"vendor checkout has local changes; not moving to {tag}", pin.version, "")
    old_pin_text = pin_file.read_text()
    co = _git(src, "checkout", "--quiet", tag)
    if co.returncode != 0:
        return UpdateResult(False, f"git checkout {tag} failed: {co.stderr.strip()}", pin.version, "")
    new_version = (src / "VERSION").read_text().strip() if (src / "VERSION").is_file() else tag.lstrip("v")
    _negpy.write_pin(_negpy.Pin(commit=_head(src), version=new_version), pin_file)
    ok, msg, deps = smoke_with_deps(arw, src, smoke)
    if not ok:
        _git(src, "checkout", "--quiet", pin.commit)
        pin_file.write_text(old_pin_text)
        return UpdateResult(
            False,
            f"!!! NegPy {new_version} FAILED smoke ({msg}) -> rolled back to {pin.version} ({pin.commit})",
            pin.version,
            pin.version,
        )
    line = (
        f"NegPy {pin.version} -> {new_version} ({_head(src)}); smoke {msg}"
        + (f"; installed {deps}" if deps else "")
        + f". Recipes graded on {pin.version} will be re-checked by the corridor"
    )
    return UpdateResult(True, "; ".join([line, *notes]), pin.version, new_version)


# ---------------------------------------------------------------------------
# state.json
# ---------------------------------------------------------------------------
def _state_path(cfg: Config) -> Path:
    return cfg.cache_dir / "state.json"


def load_state(cfg: Config) -> dict:
    p = _state_path(cfg)
    return json.loads(p.read_text()) if p.is_file() else {}


def save_state(cfg: Config, state: dict) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1) + "\n")


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def _short(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home) :] if s.startswith(home) else s


def _config_section(cfg: Config, cli_keys: frozenset[str], rep: StartReport) -> bool:
    src = config_sources(cli_keys)
    rep.add(f"config  {_short(config_file_path())}")
    ok = True
    for key in SHOWN_PATHS:
        path: Path = getattr(cfg, key)
        if path.exists():
            mark = ""
        elif key in OPTIONAL_PATHS:
            mark = f"   (not found: {OPTIONAL_PATHS[key]})"
        else:
            mark = "   <-- MISSING"
            ok = False
        rep.add(f"  {key:<12} {_short(path)}  [{src[key]}]{mark}")
    if not (cfg.look_dir / "base_template.json").is_file():
        rep.add(f"  no look in {_short(cfg.look_dir)}: run `negmcp init` (or `negmcp init --look-dir DIR`)")
        ok = False
    if not ok:
        rep.add("  fix: create the folder, or set it: `negmcp -c KEY=PATH ...`, env NEGMCP_<KEY>, or the config file")
    return ok


def _recipes_section(cfg: Config, current: str, rep: StartReport) -> None:
    from negmcp.recipe import GRADED_VERSION_KEY, RecipeError, load_recipe

    recipes = sorted(cfg.rolls_dir.glob("*_recipe.json"))
    closed = cfg.rolls_dir / CLOSED_SUBDIR
    n_closed = len(list(closed.glob("*_recipe.json"))) if closed.is_dir() else 0
    rep.add(f"recipes {len(recipes)} active in rolls_dir ({CLOSED_SUBDIR}/: {n_closed}, not checked)")
    for path in recipes:
        roll = path.name.removesuffix("_recipe.json")
        try:
            recipe = load_recipe(path)
        except (RecipeError, ValueError, OSError) as exc:
            first = exc.problems[0] if isinstance(exc, RecipeError) else str(exc)
            rep.add(f"  {roll:<20} WARN invalid: {first}")
            continue
        graded = recipe.get(GRADED_VERSION_KEY)
        flag = (
            ""
            if graded == current
            else "  ⚠ not stamped -> check with `negmcp qa`"
            if graded is None
            else "  ⚠ engine changed -> run the corridor (`negmcp qa`)"
        )
        rep.add(f"  {roll:<20} valid, graded on {graded or '-'}{flag}")


def run_start(
    cfg: Config,
    *,
    update: bool = True,
    recalibrate: bool = False,
    cli_keys: frozenset[str] = frozenset(),
    pin_file: Path | None = None,
    smoke: SmokeFn = _smoke_subprocess,
) -> StartReport:
    rep = StartReport()
    paths_ok = _config_section(cfg, cli_keys, rep)
    state = load_state(cfg)
    pin_file = pin_file or _negpy.PIN_FILE

    if (cfg.negpy_src / ".git").exists():
        upd = update_negpy(cfg.negpy_src, pin_file, _smoke_arw(cfg), fetch=update, smoke=smoke)
        rep.add(f"negpy   {upd.line}")
        if not upd.ok:
            rep.failed = True
            return rep
        current = upd.new
    else:
        rep.add(f"negpy   no git checkout at {_short(cfg.negpy_src)}: run vendor/setup.sh")
        rep.failed = True
        return rep
    if state.get("negpy_version") and state["negpy_version"] != current:
        rep.add(f"        engine changed since last start: {state['negpy_version']} -> {current}")

    if cfg.refs_dir.is_dir():
        from negmcp import refs

        t = time.perf_counter()
        st = refs.ensure(force=recalibrate)
        took = f" in {time.perf_counter() - t:.1f}s" if st.corridor_why or st.house_why else ""
        rep.add(f"refs    {refs.summary(st)}{took} [corridor {st.corridor.get('calibrated_at', '?')}]")
        state["refs_hash"], state["refs_count"] = st.refs_hash, st.count
    else:
        rep.add("refs    refs_dir missing: corridor / house_look cannot be derived (QA will fail its corridor step)")

    if cfg.rolls_dir.is_dir():
        _recipes_section(cfg, current, rep)

    mcp = subprocess.run(
        [sys.executable, "-c", "import negmcp.server"], capture_output=True, text=True, timeout=120, check=False
    )
    tail = mcp.stderr.strip().splitlines()[-1:] if mcp.returncode else []
    rep.add("mcp     `negmcp serve` imports ok" if mcp.returncode == 0 else f"mcp     import FAILED: {tail}")
    rep.failed = rep.failed or mcp.returncode != 0 or not paths_ok

    state.update(negpy_version=current, last_start=datetime.now(tz=UTC).isoformat(timespec="seconds"))
    save_state(cfg, state)
    return rep
