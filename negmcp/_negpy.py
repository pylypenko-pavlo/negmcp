"""The ONE place that puts vendor/NegPy on ``sys.path`` and checks it sits on the pin.

The pin lives in ``vendor/NEGPY_PIN`` (shell-sourceable, shared with ``vendor/setup.sh``).
Importing ``negmcp.render`` calls :func:`bootstrap`; a missing or off-pin checkout fails
loudly here instead of rendering with a different engine than the one the baselines and
recipes were validated on.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from negmcp.config import REPO_ROOT, get_config

__all__ = ["NegPyPinError", "Pin", "bootstrap", "read_pin", "write_pin"]

log = logging.getLogger(__name__)
PIN_FILE = REPO_ROOT / "vendor" / "NEGPY_PIN"


class NegPyPinError(RuntimeError):
    """vendor/NegPy is missing or not checked out at the pinned commit/version."""


@dataclass(frozen=True, slots=True)
class Pin:
    commit: str
    version: str


def read_pin(path: Path = PIN_FILE) -> Pin:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, val = line.partition("=")
            values[k.strip()] = val.strip()
    try:
        return Pin(commit=values["NEGPY_COMMIT"], version=values["NEGPY_VERSION"])
    except KeyError as e:
        raise NegPyPinError(f"{path}: missing {e.args[0]}") from e


def write_pin(pin: Pin, path: Path = PIN_FILE) -> None:
    """Rewrite the NEGPY_COMMIT / NEGPY_VERSION lines of the pin file; comments are kept."""
    values = {"NEGPY_COMMIT": pin.commit, "NEGPY_VERSION": pin.version}
    lines = []
    for line in path.read_text().splitlines():
        key = line.partition("=")[0].strip()
        lines.append(f"{key}={values.pop(key)}" if key in values and not line.lstrip().startswith("#") else line)
    lines += [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n")


def _git_head(src: Path) -> str | None:
    if not (src / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        log.debug("git rev-parse failed for %s; checking VERSION only", src, exc_info=True)
        return None
    return out.stdout.strip()


_bootstrapped: Pin | None = None


def bootstrap(src: Path | None = None) -> Pin:
    """Verify the NegPy checkout against the pin and make ``import negpy`` resolve to it."""
    global _bootstrapped
    if _bootstrapped is not None:
        return _bootstrapped
    src = (src or get_config().negpy_src).resolve()
    pin = read_pin()
    fix = f"run `bash {REPO_ROOT / 'vendor' / 'setup.sh'}` (or point negpy_src at a checkout of {pin.commit})"
    if not (src / "negpy").is_dir():
        raise NegPyPinError(f"NegPy not found at {src}: {fix}")
    version = (src / "VERSION").read_text().strip() if (src / "VERSION").is_file() else "?"
    if version != pin.version:
        raise NegPyPinError(f"NegPy at {src} is version {version}, pin is {pin.version} ({PIN_FILE}): {fix}")
    head = _git_head(src)
    if head is not None and not head.startswith(pin.commit):
        raise NegPyPinError(f"NegPy at {src} is at commit {head[:10]}, pin is {pin.commit} ({PIN_FILE}): {fix}")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    _bootstrapped = pin
    return pin


def installed_version() -> str:
    return (get_config().negpy_src / "VERSION").read_text().strip()
