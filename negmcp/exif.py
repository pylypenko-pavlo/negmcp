"""Roll metadata: the ONLY parser of ``<neg_root>/<roll>/info.txt`` + EXIF/XMP stamping.

info.txt is ``key: value`` per line. Known keys: film, camera, lens, format, push,
locations, negpy_crosstalk_profile, note, developer, scanning. The scan's own EXIF describes
the digitising camera (copy-stand scanner), not the camera the film was shot with, so the
SHOOTING camera/lens come from info.txt only.

Two stampers, both idempotent:
  * :func:`batch_exif` — EXIF bytes the batch embeds at save time
    (Make/Model <- camera, LensModel <- lens, description <- film/format/developer/push/scanning,
    DateTimeOriginal <- the ARW's scan date).
  * :func:`tag_jpeg` — re-stamps an existing JPEG in place (scanner Make/Model/LensModel are
    always removed first; description <- film/push/locations; XMP-dc:Description via exiftool).
An empty or ``-`` field is never written; no defaults are invented.
"""

from __future__ import annotations

import logging
import struct
import subprocess
from enum import StrEnum
from pathlib import Path

import exifread
import piexif
from piexif import helper as piexif_helper

from negmcp.config import get_config

__all__ = [
    "RollFormat",
    "batch_exif",
    "build_description",
    "load_roll_info",
    "parse_info_txt",
    "roll_format",
    "tag_jpeg",
]

log = logging.getLogger(__name__)

# info.txt fields -> description, for tag_jpeg (film + push + locations)
DESCRIPTION_FIELDS = [("film", "Film"), ("push", "Push"), ("locations", "Locations")]
# info.txt fields -> description, for the batch-embedded EXIF
BATCH_DESCRIPTION_FIELDS = [
    ("film", "Film"),
    ("format", "Format"),
    ("developer", "Dev"),
    ("push", "Push"),
    ("scanning", "Scan"),
]


class RollFormat(StrEnum):
    HALF = "half"  # each ARW holds two half-frames, split L/R
    FF = "ff"  # one image per ARW (35mm full frame)


def _clean(value: str | None) -> str | None:
    """Trim; '-' and '' mean "no value" — never substitute a default."""
    if value is None:
        return None
    value = value.strip()
    if not value or value == "-":
        return None
    return value


def parse_info_txt(path: Path) -> dict[str, str]:
    """info.txt -> {lower-case key: value}. Missing file -> {} (not an error)."""
    info: dict[str, str] = {}
    if not path.exists():
        return info
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key and value:
            info[key] = value
    return info


def load_roll_info(roll: str | Path) -> dict[str, str]:
    """roll = name (-> neg_root/<roll>/info.txt), a roll directory, or the info.txt itself."""
    roll_path = Path(roll).expanduser()
    if roll_path.is_file():
        info_path = roll_path
    elif roll_path.is_dir():
        info_path = roll_path / "info.txt"
    else:
        info_path = get_config().neg_root / str(roll) / "info.txt"
    return parse_info_txt(info_path)


def roll_format(info: dict[str, str]) -> RollFormat | None:
    """``format:`` line -> RollFormat. "half-frame (...)" -> HALF; "35mm"/"full..."/"ff" -> FF."""
    fmt = (_clean(info.get("format")) or "").lower()
    if fmt.startswith("half"):
        return RollFormat.HALF
    if fmt.startswith(("35mm", "full", "ff")):
        return RollFormat.FF
    return None


def _split_make_model(camera: str) -> tuple[str | None, str]:
    parts = camera.split(" ", 1)
    make = parts[0] if parts[0] else None
    model = parts[1] if len(parts) > 1 else camera
    return make, model


def build_description(info: dict[str, str], fields: list[tuple[str, str]] = DESCRIPTION_FIELDS) -> str:
    parts = []
    for key, label in fields:
        v = _clean(info.get(key))
        if v:
            parts.append(f"{label}: {v}")
    return "; ".join(parts)


def _arw_datetime(arw_path: Path) -> str | None:
    """DateTimeOriginal of the scan (the copy-stand capture), read with exifread."""
    try:
        with arw_path.open("rb") as fh:
            tags = exifread.process_file(fh, details=False)
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        log.warning("could not read EXIF date from %s", arw_path, exc_info=True)
        return None
    v = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    return str(v) if v else None


def batch_exif(info: dict[str, str], arw_path: Path) -> bytes:
    """EXIF block the batch embeds in every rendered JPEG."""
    desc = build_description(info, BATCH_DESCRIPTION_FIELDS)
    zeroth: dict[int, bytes] = {}
    exif_ifd: dict[int, bytes] = {}
    camera = _clean(info.get("camera"))
    if camera:
        make, model = _split_make_model(camera)
        zeroth[piexif.ImageIFD.Make] = (make or "").encode()
        zeroth[piexif.ImageIFD.Model] = model.encode()
    if desc:
        zeroth[piexif.ImageIFD.ImageDescription] = desc.encode("utf-8", "replace")
    zeroth[piexif.ImageIFD.Software] = b"negmcp"
    lens = _clean(info.get("lens"))
    if lens:
        exif_ifd[piexif.ExifIFD.LensModel] = lens.encode("utf-8", "replace")
    dt = _arw_datetime(arw_path)
    if dt:
        exif_ifd[piexif.ExifIFD.DateTimeOriginal] = dt.encode()
    if desc:
        exif_ifd[piexif.ExifIFD.UserComment] = piexif_helper.UserComment.dump(desc, encoding="unicode")
    return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "1st": {}, "GPS": {}, "Interop": {}})


def _read_arw_scan_datetime(arw_path: Path) -> str | None:
    """DateTimeOriginal of the scan via exiftool. Scanner Make/Model are NOT copied."""
    try:
        out = subprocess.run(
            ["exiftool", "-s3", "-DateTimeOriginal", str(arw_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        log.warning("exiftool could not read %s", arw_path, exc_info=True)
        return None
    return out or None


def _load_exif_dict(jpeg_path: Path) -> dict:
    try:
        return piexif.load(str(jpeg_path))
    except (piexif.InvalidImageDataError, ValueError, struct.error):
        return {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}}


def tag_jpeg(jpeg_path: str | Path, roll: str | Path, arw_path: str | Path | None = None) -> dict[str, str]:
    """Stamp ``jpeg_path`` with the roll's info.txt (+ the ARW scan date). Returns what was written."""
    jpeg_path = Path(jpeg_path)
    info = load_roll_info(roll)

    exif_dict = _load_exif_dict(jpeg_path)
    zeroth = exif_dict.get("0th", {})
    exif_ifd = exif_dict.get("Exif", {})

    # scanner Make/Model/LensModel always go: replaced by the shooting camera, or simply
    # not re-written when info.txt is empty.
    zeroth.pop(piexif.ImageIFD.Make, None)
    zeroth.pop(piexif.ImageIFD.Model, None)
    exif_ifd.pop(piexif.ExifIFD.LensModel, None)

    written: dict[str, str] = {}

    camera = _clean(info.get("camera"))
    if camera:
        make, model = _split_make_model(camera)
        if make:
            zeroth[piexif.ImageIFD.Make] = make.encode("utf-8")
            written["Make"] = make
        zeroth[piexif.ImageIFD.Model] = model.encode("utf-8")
        written["Model"] = model

    lens = _clean(info.get("lens"))
    if lens:
        exif_ifd[piexif.ExifIFD.LensModel] = lens.encode("utf-8")
        written["LensModel"] = lens

    description = build_description(info)
    if description:
        zeroth[piexif.ImageIFD.ImageDescription] = description.encode("utf-8", "replace")
        exif_ifd[piexif.ExifIFD.UserComment] = piexif_helper.UserComment.dump(description, encoding="unicode")
        written["ImageDescription"] = description
    else:
        zeroth.pop(piexif.ImageIFD.ImageDescription, None)
        exif_ifd.pop(piexif.ExifIFD.UserComment, None)

    if arw_path is not None:
        dto = _read_arw_scan_datetime(Path(arw_path))
        if dto:
            exif_ifd[piexif.ExifIFD.DateTimeOriginal] = dto.encode("ascii")
            written["DateTimeOriginal"] = dto

    exif_dict["0th"] = zeroth
    exif_dict["Exif"] = exif_ifd
    piexif.insert(piexif.dump(exif_dict), str(jpeg_path))

    if description:
        _write_xmp_description(jpeg_path, description)

    return written


def _write_xmp_description(jpeg_path: Path, description: str) -> None:
    """XMP-dc:Description mirror. Plain assignment (=), not (+=) -> idempotent."""
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-q", f"-XMP-dc:Description={description}", str(jpeg_path)],
        check=True,
    )
