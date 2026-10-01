"""Film-strip contact sheet: lays roll finals out as real negative strips —
black rebate, sprocket perforations along top+bottom bands, orange edge-print
(film name repeated) + frame numbers in the bottom rebate. Authentic proof-sheet look.

CLI: ``negmcp contact <roll> [--film NAME] [--out PATH] [--per-row N]``.
"""

from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = ["build"]

FRAME_H = 460  # image height inside the strip
GAP = 14  # black rebate between frames
BAND = 46  # sprocket band height (top and bottom)
MARGIN = 60  # paper margin around the whole sheet
ROWGAP = 26  # black gap between strips
PAPER = (24, 22, 20)  # near-black proof-paper
REBATE = (12, 11, 10)  # film rebate (slightly darker than paper)
PERF = (206, 202, 190)  # sprocket hole (light film base showing through)
EDGE = (232, 138, 46)  # orange edge-print ink

# sprocket geometry
SW, SH, SPER = 16, 24, 38  # hole width, height, period


@cache
def _font(size):
    for p in (
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    ):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def perf_band(draw, x0, x1, y0, h):
    """Draw a row of rounded sprocket holes centered vertically in a band."""
    cy = y0 + h // 2
    x = x0 + SPER // 2
    while x + SW <= x1:
        draw.rounded_rectangle([x, cy - SH // 2, x + SW, cy + SH // 2], radius=4, fill=PERF)
        x += SPER


def build(review_dir: Path, out: Path, film: str = "FILM", per_row: int = 5) -> Path:
    """Render every ``review_dir/*.jpg`` (sorted) into a film-strip sheet at ``out``."""
    paths = sorted(p for p in review_dir.glob("*.jpg") if not p.name.startswith("_"))
    if not paths:
        raise FileNotFoundError(f"no jpgs in {review_dir}")
    FILM, PER_ROW = film.upper(), per_row
    F_EDGE, F_NUM = _font(20), _font(26)

    # scale each frame to FRAME_H, remember widths
    frames = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        w = max(1, round(im.width * FRAME_H / im.height))
        frames.append((p.stem, im.resize((w, FRAME_H), Image.LANCZOS)))

    rows = [frames[i : i + PER_ROW] for i in range(0, len(frames), PER_ROW)]
    strip_h = BAND * 2 + FRAME_H
    # sheet width = widest strip
    sheet_w = 0
    for row in rows:
        w = GAP + sum(im.width + GAP for _, im in row)
        sheet_w = max(sheet_w, w)
    sheet_w = max(sheet_w, 900) + MARGIN * 2
    sheet_h = MARGIN * 2 + len(rows) * strip_h + (len(rows) - 1) * ROWGAP

    sheet = Image.new("RGB", (sheet_w, sheet_h), PAPER)
    d = ImageDraw.Draw(sheet)

    y = MARGIN
    fnum = 1
    for row in rows:
        strip_w = GAP + sum(im.width + GAP for _, im in row)
        x0 = MARGIN
        x1 = x0 + strip_w
        # rebate (film) background for the whole strip
        d.rectangle([x0, y, x1, y + strip_h], fill=REBATE)
        # top + bottom sprocket bands
        perf_band(d, x0, x1, y, BAND)
        perf_band(d, x0, x1, y + BAND + FRAME_H, BAND)
        # frames
        fx = x0 + GAP
        fy = y + BAND
        for stem, im in row:
            sheet.paste(im, (fx, fy))
            # frame number in orange, bottom-left corner of the image (edge-code style)
            d.text((fx + 6, fy + FRAME_H - 30), f"{fnum}", font=F_NUM, fill=EDGE)
            # stem (real file id) small, top-left
            d.text((fx + 6, fy + 6), stem, font=F_EDGE, fill=(235, 235, 225))
            fnum += 1
            fx += im.width + GAP
        # edge-print: film name repeated along the bottom band
        label = f"  {FILM}   " + "•   " * 2
        tw = d.textlength(label, font=F_EDGE)
        ex = x0 + 8
        while ex < x1 - 20:
            d.text((ex, y + BAND + FRAME_H + (BAND - 20) // 2), label, font=F_EDGE, fill=EDGE)
            ex += int(tw)
        y += strip_h + ROWGAP

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=92)
    print(f"{out.name}: {len(frames)} frames, {len(rows)} strips -> {out} ({sheet_w}x{sheet_h})")
    return out
