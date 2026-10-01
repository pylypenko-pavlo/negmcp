"""Runtime configuration.

Precedence (highest first): CLI flag (``negmcp -c key=value``) > env ``NEGMCP_<KEY>`` >
``$XDG_CONFIG_HOME/negmcp/config.toml`` (default ``~/.config/negmcp/config.toml``; or
``NEGMCP_CONFIG`` / ``--config``) > built-in defaults.

Built-in defaults follow the XDG base directories: the look (``look_dir``) and the roll
recipes (``rolls_dir``) under ``$XDG_DATA_HOME/negmcp`` (``~/.local/share/negmcp``), caches
under ``$XDG_CACHE_HOME/negmcp`` (``~/.cache/negmcp``), and the photo folders (scans, refs,
review, approved finals) under one workspace folder, ``~/negmcp``. See docs/config.md.

The CLI applies its flags by exporting them as ``NEGMCP_<KEY>`` before anything reads the
config, so spawned render workers (multiprocessing "spawn" re-imports everything) resolve
exactly the same values as the parent.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = [
    "KEYS",
    "REPO_ROOT",
    "Config",
    "ConfigError",
    "config_file_path",
    "config_sources",
    "default_config_file",
    "get_config",
    "load_config",
    "save_config_value",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PREFIX = "NEGMCP_"
WORKSPACE = Path("~/negmcp")  # default parent of neg_root / refs_dir / review_root / pos_root
CLOSED_SUBDIR = "closed"  # rolls_dir/closed/: finished rolls, found by name but not checked by `start`


class ConfigError(ValueError):
    """Unknown key or uncoercible value in a config source."""


@dataclass(frozen=True, slots=True)
class Config:
    negpy_src: Path
    look_dir: Path
    rolls_dir: Path
    house_look: Path
    refs_dir: Path
    neg_root: Path
    review_root: Path
    pos_root: Path
    work_dir: Path
    cache_dir: Path
    edits_db: Path
    iter_long_edge: int
    preview_long_edge: int
    workers: int
    log_level: str

    def roll_dir(self, roll: str) -> Path:
        """Roll name -> ``neg_root/<roll>``; an explicit path is used as-is."""
        p = Path(roll).expanduser()
        return p if (os.sep in roll or roll.startswith("~")) else self.neg_root / roll

    def recipe_path(self, roll: str) -> Path:
        """Roll name -> ``rolls_dir/<roll>_recipe.json``, else ``rolls_dir/closed/<roll>_recipe.json``
        (closed rolls); an explicit file path is used as-is."""
        p = Path(roll).expanduser()
        if p.suffix == ".json" or os.sep in roll:
            return p
        name = f"{roll}_recipe.json"
        active = self.rolls_dir / name
        closed = self.rolls_dir / CLOSED_SUBDIR / name
        return closed if not active.exists() and closed.exists() else active

    def review_dir(self, roll: str) -> Path:
        return self.review_root / Path(roll).name

    def approved_dir(self, roll: str) -> Path:
        """Roll name -> ``pos_root/<roll>``: the roll's approved finals (what `qa` compares to)."""
        return self.pos_root / Path(roll).name


_PATH_KEYS = (
    "negpy_src",
    "look_dir",
    "rolls_dir",
    "house_look",
    "refs_dir",
    "neg_root",
    "review_root",
    "pos_root",
    "work_dir",
    "cache_dir",
    "edits_db",
)
_INT_KEYS = ("iter_long_edge", "preview_long_edge", "workers")
_STR_KEYS = ("log_level",)
KEYS = frozenset(_PATH_KEYS + _INT_KEYS + _STR_KEYS)


def _read_file(path: Path) -> dict[str, object]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    unknown = set(data) - KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown config key(s) {sorted(unknown)}; known: {sorted(KEYS)}")
    return data


def _coerce(key: str, value: object) -> Path | int | str:
    if key in _PATH_KEYS:
        return Path(str(value)).expanduser()
    if key in _INT_KEYS:
        try:
            return int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{key}: expected an integer, got {value!r}") from e
    return str(value)


def _xdg(var: str, fallback: str) -> Path:
    """``$var`` when set to an absolute path (XDG base-directory rule), else ``~/<fallback>``."""
    value = os.environ.get(var, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def default_config_file() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "negmcp" / "config.toml"


def config_file_path() -> Path:
    explicit = os.environ.get(f"{ENV_PREFIX}CONFIG")
    return Path(explicit).expanduser() if explicit else default_config_file()


def config_sources(cli_keys: frozenset[str] = frozenset()) -> dict[str, str]:
    """Where each key's value comes from: ``cli`` | ``env`` | ``file`` | ``default``.

    ``cli_keys`` are the keys the CLI exported as env (see cli._export_overrides) — after
    export they are indistinguishable from env otherwise."""
    path = config_file_path()
    in_file = set(_read_file(path)) if path.is_file() else set()
    out = {}
    for key in sorted(KEYS):
        if key in cli_keys:
            out[key] = "cli"
        elif os.environ.get(f"{ENV_PREFIX}{key.upper()}"):
            out[key] = "env"
        elif key in in_file:
            out[key] = "file"
        else:
            out[key] = "default"
    return out


def save_config_value(key: str, value: str, path: Path | None = None) -> Path:
    """Set ``key = "value"`` in the config file (replacing an existing assignment, else
    appending); other lines and comments are kept. The result must still parse."""
    if key not in KEYS:
        raise ConfigError(f"unknown config key {key!r}; known: {sorted(KEYS)}")
    path = path or config_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    line = f"{key} = {json.dumps(value)}"
    for i, old in enumerate(lines):
        if old.split("=", 1)[0].strip() == key and not old.lstrip().startswith("#"):
            lines[i] = line
            break
    else:
        lines.append(line)
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_config(overrides: Mapping[str, object] | None = None, config_file: Path | None = None) -> Config:
    """Resolve a Config from all sources. ``overrides`` play the role of CLI flags."""
    overrides = dict(overrides or {})
    unknown = set(overrides) - KEYS
    if unknown:
        raise ConfigError(f"unknown config key(s) {sorted(unknown)}; known: {sorted(KEYS)}")

    file_path = config_file or config_file_path()
    raw: dict[str, object] = _read_file(file_path) if file_path.is_file() else {}
    for key in KEYS:
        env_val = os.environ.get(f"{ENV_PREFIX}{key.upper()}")
        if env_val:
            raw[key] = env_val
    raw.update(overrides)
    v = {k: _coerce(k, val) for k, val in raw.items()}

    data_dir = _xdg("XDG_DATA_HOME", ".local/share") / "negmcp"
    workspace = WORKSPACE.expanduser()
    negpy_src = v.get("negpy_src", REPO_ROOT / "vendor" / "NegPy")
    look_dir = v.get("look_dir", data_dir / "look")
    cache_dir = v.get("cache_dir", _xdg("XDG_CACHE_HOME", ".cache") / "negmcp")
    return Config(
        negpy_src=Path(negpy_src),
        look_dir=Path(look_dir),
        rolls_dir=Path(v.get("rolls_dir", data_dir / "rolls")),
        house_look=Path(v.get("house_look", Path(look_dir) / "house_look.json")),
        refs_dir=Path(v.get("refs_dir", workspace / "refs")),
        neg_root=Path(v.get("neg_root", workspace / "neg")),
        review_root=Path(v.get("review_root", workspace / "review")),
        pos_root=Path(v.get("pos_root", workspace / "pos")),
        work_dir=Path(v.get("work_dir", Path(cache_dir) / "work")),
        cache_dir=Path(cache_dir),
        edits_db=Path(v.get("edits_db", Path.home() / "Documents" / "NegPy" / "edits.db")),
        iter_long_edge=int(v.get("iter_long_edge", 2200)),
        preview_long_edge=int(v.get("preview_long_edge", 1024)),
        workers=int(v.get("workers", max(1, (os.cpu_count() or 4) - 2))),
        log_level=str(v.get("log_level", "INFO")).upper(),
    )


@cache
def get_config() -> Config:
    """Process-wide config, resolved once (env/CLI are applied before first use)."""
    return load_config()
