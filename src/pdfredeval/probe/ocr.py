"""OCR of the output - the layer that catches what every parser-based check misses.

A translucent cover, a hairline box, a rectangle drawn just short of a descender: none of
these leave the value in the content stream, and all of them leave it legible. Only
reading the rendered page finds them.

docs/metrics/redaction.md flags this as the layer that quietly fails open, and names the
two ways:

* **Both polarities.** A dark rectangle painted over live text leaves white-on-black,
  which is exactly what a dark-on-light binariser drops. An OCR pass that cannot read
  inverse text reports "covered" over plainly legible words, scoring a leak as a clean
  redaction.
* **Every rotation the page contains.** A quarter-turned or stacked run that survives is
  invisible to a single horizontal pass.

Every word is kept with its box, mapped back to the unrotated page, so the scorer can
tell a value read at a probe from the same value read at its neighbour.

And each probe's own box is read on its own. A whole-page pass segments a packed probe
sheet badly and garbles words that are plainly legible; a crop of one box, enlarged
when the type is small, is the question asked directly: is there readable text *here*?

And when no engine is installed, the layer says so. "No text found" from a pass that
never ran is the same sentence as "no leak", and they must never be confused.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .. import engines
from ..align import Frame
from ..types import Case
from .base import Layer, Reading, Span, unavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: Orientation levels map onto the extra passes they need. `vertical` is not `rot90`:
#: its glyphs are already upright, so no rotation recovers it - but a rotated pass costs
#: little and a stacked column occasionally reads better sideways.
_ROTATION_FOR = {
    "horizontal": (), "rot90": (90,), "rot270": (270,), "rot180": (180,),
    "skew": (), "vertical": (90, 270),
}


def rotations(case: Case) -> tuple[int, ...]:
    """The rotations this page's own probes call for, always including upright."""
    needed = {0}
    for probe in case.probes:
        needed.update(_ROTATION_FOR.get(probe.conditions.orientation, ()))
    return tuple(sorted(needed))


def passes(image: Image, turns: Iterable[int]) -> list[tuple[str, int, Image]]:
    """Every (label, rotation, image) an OCR sweep should read: each rotation, both
    polarities."""
    from PIL import ImageOps

    sweep: list[tuple[str, int, Image]] = []
    for angle in turns:
        rotated = image if angle == 0 else image.rotate(-angle, expand=True)
        sweep.append((f"rot{angle}", angle, rotated))
        sweep.append((f"rot{angle}-inverse", angle,
                      ImageOps.invert(rotated.convert("RGB"))))
    return sweep


def unrotate(
    box: tuple[float, float, float, float], angle: int, size: tuple[int, int]
) -> tuple[float, float, float, float]:
    """A box read off a pass turned clockwise by `angle`, back on the upright page.

    `size` is the upright page's (width, height). Quarter turns only, so the mapping is
    exact and a box stays a box.
    """
    width, height = size
    u0, v0, u1, v1 = box
    if angle == 90:
        return (v0, height - u1, v1, height - u0)
    if angle == 180:
        return (width - u1, height - v1, width - u0, height - v0)
    if angle == 270:
        return (width - v1, u0, width - v0, u1)
    return box


def read(image: Image | None, case: Case, *, enabled: bool = True) -> Reading:
    """Read the rendered output at both polarities and every rotation it needs.

    The spans are in the output render's pixels; `probe()` places them on the input.
    """
    if not enabled:
        return unavailable(Layer.OCR, "OCR disabled for this run (--no-ocr)")
    if image is None:
        return unavailable(Layer.OCR, "no rasteriser, so the page could not be read")
    if engines.ocr_engine() is None:
        return unavailable(
            Layer.OCR,
            "no OCR engine on PATH; this layer was not checked. A tool's covers were "
            "not read back, so a translucent or hairline cover would go unnoticed.",
        )

    turns = rotations(case)
    chunks: list[str] = []
    spans: list[Span] = []
    detail: dict[str, Any] = {"passes": [], "rotations": list(turns)}
    for label, angle, rendered in passes(image, turns):
        try:
            words = engines.ocr_words(rendered)
        except engines.OcrUnavailable as exc:  # pragma: no cover - engine died mid-run
            return unavailable(Layer.OCR, str(exc))
        lines: list[str] = []
        for i, word in enumerate(words):
            last = i + 1 == len(words) or words[i + 1].line != word.line
            spans.append(Span(word.text + ("\n" if last else " "),
                              unrotate(word.box, angle, image.size)))
            lines.append(word.text + ("\n" if last else " "))
        text = "".join(lines)
        chunks.append(text)
        detail["passes"].append({"pass": label, "chars": len(text.strip())})
    return Reading(Layer.OCR, "\n".join(chunks), detail=detail, spans=tuple(spans))


#: Tesseract reads best with a line of text some tens of pixels tall; a 4pt value at
#: 300 DPI is a third of that.
_MIN_CROP_PX = 48
#: Blank margin round a crop. Tesseract drops text that touches the image edge.
_BORDER_PX = 20


def read_boxes(image: Image, case: Case, frame: Frame, *, margin_px: int) -> tuple[Span, ...]:
    """Read every text probe's box of an output already warped into the input's frame.

    One span per probe, owned by that probe, holding everything read off its box at
    both polarities and at the rotations its orientation needs. The engine must exist;
    the caller has already asked.
    """
    from PIL import Image as PILImage
    from PIL import ImageOps

    spans: list[Span] = []
    for probe in case.probes:
        if probe.bbox is None or probe.bbox.page != 0 or not probe.units:
            continue
        x0, y0, x1, y1 = frame.box(probe.bbox)
        box = (x0 - margin_px, y0 - margin_px, x1 + margin_px, y1 + margin_px)
        crop = image.crop(box).convert("RGB")
        scale = _MIN_CROP_PX / max(1, min(crop.size))
        if scale > 1:
            crop = crop.resize((round(crop.width * scale), round(crop.height * scale)),
                               PILImage.Resampling.LANCZOS)

        read: list[str] = []
        for angle in (0, *_ROTATION_FOR.get(probe.conditions.orientation, ())):
            turned = crop if angle == 0 else crop.rotate(-angle, expand=True)
            for view in (turned, ImageOps.invert(turned)):
                framed = ImageOps.expand(view, _BORDER_PX, fill=view.getpixel((0, 0)))
                words = engines.ocr_words(framed)
                read.append(" ".join(word.text for word in words) + "\n")
        spans.append(Span("".join(read), tuple(float(v) for v in box),  # type: ignore[arg-type]
                          frozenset({probe.id})))
    return tuple(spans)
