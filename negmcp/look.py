"""``negmcp init`` — a starter look from the template shipped in the package.

The look (``look_dir``) is the user's: ``base_template.json`` (the universal base every new
recipe starts from), ``film_deltas.json`` (per-stock corrections) and ``house_look.json``
(the hand-written ``taste`` + the refs-derived target, filled in by ``negmcp refs`` /
``negmcp start``). The package only ships a neutral starting point
(``negmcp/data/look_template/``); existing files are never overwritten.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["TEMPLATE_FILES", "init_look"]

TEMPLATE_FILES = ("base_template.json", "film_deltas.json", "house_look.json")


def init_look(look_dir: Path, rolls_dir: Path) -> list[tuple[Path, bool]]:
    """Copy the template files missing from ``look_dir`` and create ``rolls_dir``.

    Returns ``(path, written)`` per template file; ``written`` is False for a file that
    already existed and was kept."""
    template = resources.files("negmcp") / "data" / "look_template"
    look_dir.mkdir(parents=True, exist_ok=True)
    rolls_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for name in TEMPLATE_FILES:
        dest = look_dir / name
        if dest.exists():
            out.append((dest, False))
            continue
        dest.write_text((template / name).read_text(encoding="utf-8"), encoding="utf-8")
        out.append((dest, True))
    return out
