"""Ground truth drawn over the output, so a disputed result can be looked at.

docs/core.md asks for exactly this under the notebook layer: *overlay ground truth on
output, eyeball disagreements*. It is the artefact that settles an argument. A vendor
told "probe t023 leaked" has to take our word for it; a vendor handed a page with a red
box round their own output does not.

One image per run: the tool's output, warped back into the input's frame, with every
probe box drawn and labelled by outcome.

Colour is never the only channel. Each box carries a glyph and the probe's id, because
a reader who cannot separate the red from the green still has to be able to read the
page.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..align import Frame
from ..probe import Observations
from ..types import Case
from .palette import LIGHT, Mode, outcome_colour, outcome_glyph, outcome_label

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: Overlays are looked at, not measured, so they render smaller than the scoring pass.
OVERLAY_DPI = 110

#: Box outline, in pixels. Thin: the page underneath is the evidence.
STROKE = 2

#: Outcomes worth a label. A caption on all eighty-six probes buries the handful the
#: reader is actually looking for, and each chip covers a piece of the page underneath.
#: Correct outcomes keep their box; the ones in question get named.
LABELLED = frozenset({"FN", "FP", "undecided"})


@dataclass(frozen=True, slots=True)
class Overlays:
    """What was drawn, and where it went."""

    overlay: Path | None = None

    def paths(self) -> list[Path]:
        return [self.overlay] if self.overlay is not None else []


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def draw_overlay(
    page: Image,
    frame: Frame,
    rows: list[dict[str, Any]],
    *,
    mode: Mode = LIGHT,
) -> Image:
    """Draw one labelled box per probe onto a rendered page."""
    from PIL import ImageDraw

    canvas = page.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    font = _font(11)

    for row in rows:
        box = row.get("bbox")
        if box is None:
            continue
        x0, y0, x1, y1 = frame.box(box)
        colour = _hex_to_rgb(outcome_colour(str(row["outcome"]), mode))
        draw.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=colour, width=STROKE)

        if str(row["outcome"]) not in LABELLED:
            continue

        # Glyph plus probe id, above the box where there is room and below where there
        # is not, so a label is never clipped by the page edge.
        caption = f"{outcome_glyph(str(row['outcome']))} {row['probe_id']}"
        text_y = y0 - 13 if y0 > 15 else y1 + 2
        left, top, right, bottom = draw.textbbox((x0, text_y), caption, font=font)
        draw.rectangle([left - 2, top - 1, right + 2, bottom + 1],
                       fill=_hex_to_rgb(mode.surface))
        draw.text((x0, text_y), caption, fill=colour, font=font)

    return _with_legend(canvas, rows, mode)


def _with_legend(page: Image, rows: list[dict[str, Any]], mode: Mode) -> Image:
    """A key strip under the page. The image travels; the legend has to travel with it."""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    present: list[str] = []
    for row in rows:
        outcome = str(row["outcome"])
        if row.get("bbox") is not None and outcome not in present:
            present.append(outcome)
    present.sort(key=lambda o: (o not in LABELLED, o))
    if not present:
        return page

    strip = 30
    canvas = PILImage.new("RGB", (page.width, page.height + strip),
                          _hex_to_rgb(mode.plane))
    canvas.paste(page, (0, 0))
    draw = ImageDraw.Draw(canvas)
    font = _font(12)
    x = 12
    for outcome in present:
        colour = _hex_to_rgb(outcome_colour(outcome, mode))
        named = " (boxed and named)" if outcome in LABELLED else " (boxed)"
        text = f"{outcome_glyph(outcome)} {outcome} - {outcome_label(outcome)}{named}"
        draw.rectangle([x, page.height + 10, x + 10, page.height + 20], outline=colour,
                       width=STROKE)
        draw.text((x + 16, page.height + 9), text, fill=_hex_to_rgb(mode.ink_secondary),
                  font=font)
        x += 22 + int(draw.textlength(text, font=font))
    return canvas


def render(
    case: Case,
    output_pdf: bytes,
    rows: list[dict[str, Any]],
    out_dir: Path | str,
    *,
    dpi: int = OVERLAY_DPI,
    mode: Mode = LIGHT,
    prefix: str | None = None,
) -> Overlays:
    """Render the overlay for one run. Skips silently only when there is no rasteriser.

    The output is warped back into the input's frame first, so a turned or resized page
    lines up with the boxes ground truth records - which is the aligner's whole job, and
    the reason the overlay is trustworthy on a rescanned page at all.
    """
    from .. import engines
    from ..probe import probe as probe_output
    from ..thresholds import DEFAULTS

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = prefix or case.case_id

    try:
        source = engines.render(case.pdf_bytes, dpi=dpi)
    except engines.EngineUnavailable:
        return Overlays()

    observations = probe_output(case, output_pdf, thresholds=DEFAULTS.but(dpi=dpi),
                                ocr_enabled=False)
    frame = Frame.of(case, source, dpi=dpi)

    page = (
        observations.alignment.warp(observations.output_render, source.size)
        if observations.output_render is not None else source
    )
    overlay_path = out / f"{stem}-overlay.png"
    draw_overlay(page, frame, rows, mode=mode).save(overlay_path)

    return Overlays(overlay=overlay_path)
