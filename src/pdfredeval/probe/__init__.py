"""The probers: everything observable about one delivered output, gathered once.

Each module here reads one surface. This file runs them all and hands the scorer a single
`Observations` bundle, which matters for a mundane reason: rendering a page at 300 DPI
and running six OCR passes over it is the expensive part of scoring, and it is the same
work for all eighty probes on the page. Probe once, decide eighty times.

Nothing here interprets what it finds. `unavailable` is carried through untouched, so the
scorer can tell "this layer says the value is gone" from "this layer could not be read" -
a distinction the whole design rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .. import engines
from ..align import Alignment, Frame, align
from ..thresholds import DEFAULTS, Thresholds
from ..types import Case
from . import gates as _gates
from . import images as _images
from . import mask as _mask
from . import metadata as _metadata
from . import ocr as _ocr
from . import revisions as _revisions
from . import text as _text
from .base import Layer, Reading, Span, unavailable
from .gates import Survivability
from .images import EmbeddedImage
from .mask import ChangeMask
from .text import UnreadablePdf

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image


@dataclass(frozen=True, slots=True)
class Observations:
    """Everything the probers found in one output, keyed by layer."""

    readings: dict[Layer, Reading]
    alignment: Alignment
    frame: Frame
    mask: ChangeMask | None
    survivability: Survivability
    output_images: tuple[EmbeddedImage, ...] = ()
    input_images: tuple[EmbeddedImage, ...] = ()
    source_render: Any = None
    output_render: Any = None
    engines: dict[str, str | None] = field(default_factory=dict)

    def text(self, layer: Layer) -> str:
        reading = self.readings.get(layer)
        return reading.text if reading and reading.readable else ""

    def readable(self, layer: Layer) -> bool:
        reading = self.readings.get(layer)
        return bool(reading and reading.readable)

    def why_unavailable(self, layer: Layer) -> str | None:
        reading = self.readings.get(layer)
        return reading.unavailable if reading else "layer not probed"

    @property
    def unavailable_layers(self) -> dict[str, str]:
        return {
            layer.value: reading.unavailable
            for layer, reading in self.readings.items()
            if reading.unavailable is not None
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "alignment": self.alignment.to_dict(),
            "layers": [r.to_dict() for r in self.readings.values()],
            "unavailable": self.unavailable_layers,
            "survivability": self.survivability.to_dict(),
            "engines": self.engines,
            "images": {
                "input": len(self.input_images),
                "output": len(self.output_images),
            },
        }


def _render(pdf: bytes, dpi: int) -> tuple[Image | None, str | None]:
    try:
        return (engines.render(pdf, dpi=dpi), None)
    except engines.RendererUnavailable as exc:
        return (None, str(exc))
    except Exception as exc:  # noqa: BLE001 - a page that will not render is a finding
        return (None, f"the page could not be rendered: {exc}")


def _place(
    spans: list[Span] | tuple[Span, ...],
    alignment: Alignment,
    boxes: dict[str, tuple[float, float, float, float]],
) -> tuple[Span, ...]:
    """Spans in output-render pixels, moved onto the input and tagged with their probes."""
    placed: list[Span] = []
    for span in spans:
        x0, y0, x1, y1 = span.box
        corners = [alignment.apply(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        box = (min(xs), min(ys), max(xs), max(ys))
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        owners = frozenset(
            probe_id for probe_id, (bx0, by0, bx1, by1) in boxes.items()
            if bx0 <= cx <= bx1 and by0 <= cy <= by1
        )
        placed.append(Span(span.text, box, owners))
    return tuple(placed)


def _locate(
    readings: dict[Layer, Reading],
    case: Case,
    output_pdf: bytes,
    output: Image | None,
    alignment: Alignment,
    frame: Frame,
    thresholds: Thresholds,
) -> None:
    """Give the content-stream, optional-content and OCR readings their positions.

    Only on a confident alignment: text placed through a mapping nobody trusts would be
    attributed to the wrong probe, which is worse than attributing it to none. Without
    it the spans are dropped and every probe is judged against the whole page, as before.
    """
    if output is None or not alignment.confident:
        for layer, reading in readings.items():
            if reading.spans is not None:
                readings[layer] = replace(reading, spans=None)
        return

    margin = thresholds.locate_margin_pt * frame.dpi / 72.0
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for item in case.probes:
        if item.bbox is None or item.bbox.page != 0:
            continue
        x0, y0, x1, y1 = frame.box(item.bbox)
        boxes[item.id] = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)

    located = _text.located_text(output_pdf, output.size)
    if located is not None:
        for layer, spans in zip((Layer.CONTENT_STREAM, Layer.OPTIONAL_CONTENT), located,
                                strict=True):
            if readings[layer].readable:
                readings[layer] = replace(readings[layer],
                                          spans=_place(spans, alignment, boxes))
    ocr = readings[Layer.OCR]
    if ocr.spans is not None:
        readings[Layer.OCR] = replace(ocr, spans=_place(ocr.spans, alignment, boxes))


def _read_boxes(
    readings: dict[Layer, Reading],
    case: Case,
    change: ChangeMask | None,
    frame: Frame,
    thresholds: Thresholds,
) -> None:
    """Add a per-probe read of each box to the OCR layer.

    Needs the output warped onto the input, which only exists when the alignment was
    trusted - and a located OCR layer, which says the same.
    """
    ocr = readings[Layer.OCR]
    if not ocr.readable or ocr.spans is None or change is None or change.after is None:
        return
    margin = int(round(thresholds.locate_margin_pt * frame.dpi / 72.0))
    try:
        boxes = _ocr.read_boxes(change.after, case, frame, margin_px=margin)
    except engines.OcrUnavailable as exc:  # pragma: no cover - engine died mid-run
        readings[Layer.OCR] = unavailable(Layer.OCR, str(exc))
        return
    readings[Layer.OCR] = replace(
        ocr, spans=ocr.spans + boxes, detail={**ocr.detail, "boxes": len(boxes)},
    )


def probe(
    case: Case,
    output_pdf: bytes,
    *,
    thresholds: Thresholds = DEFAULTS,
    ocr_enabled: bool = True,
) -> Observations:
    """Read every observable layer of `output_pdf` against `case`.

    The expensive stages - two renders and the OCR sweep - happen once here, and every
    probe on the page is decided from the result.
    """
    dpi = thresholds.dpi
    readings: dict[Layer, Reading] = {}

    for reading in _text.read(output_pdf):
        readings[reading.layer] = reading
    readings[Layer.PRIOR_REVISION] = _revisions.read(output_pdf)
    readings[Layer.METADATA] = _metadata.read(output_pdf)

    image_reading, output_images = _images.read(output_pdf)
    readings[Layer.IMAGE_XOBJECT] = image_reading
    input_images = _images.reference_hashes(case.pdf_bytes)

    source, source_problem = _render(case.pdf_bytes, dpi)
    output, output_problem = _render(output_pdf, dpi)
    frame = Frame.of(case, source, dpi=dpi)

    alignment = (
        align(case, output, frame, source=source,
              max_residual_px=thresholds.max_residual_px)
        if output is not None
        else Alignment(
            matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            method="page_box", residual_px=float("inf"), rotation_deg=0.0, scale=1.0,
            mirrored=False, found=(), confident=False,
            note=output_problem or "the output could not be rendered",
        )
    )

    if source is None or output is None:
        readings[Layer.RENDERED_PIXELS] = unavailable(
            Layer.RENDERED_PIXELS, source_problem or output_problem or "no rasteriser"
        )
        change: ChangeMask | None = None
    else:
        readings[Layer.RENDERED_PIXELS], change = _mask.read(
            source, output, alignment, frame, delta_px=thresholds.delta_px,
            morph_radius=thresholds.morph_radius,
            page_rewritten_fraction=thresholds.page_rewritten_fraction,
        )

    readings[Layer.OCR] = _ocr.read(output, case, enabled=ocr_enabled)
    _locate(readings, case, output_pdf, output, alignment, frame, thresholds)
    _read_boxes(readings, case, change, frame, thresholds)

    survivability = _gates.check(
        case, output_pdf, _gates.case_text(case),
        readings[Layer.CONTENT_STREAM].text + readings[Layer.OPTIONAL_CONTENT].text,
        min_text_retention=thresholds.min_text_retention,
        min_char_survival=thresholds.min_char_survival,
        geometry_tolerance=thresholds.geometry_tolerance,
    )

    return Observations(
        readings=readings,
        alignment=alignment,
        frame=frame,
        mask=change,
        survivability=survivability,
        output_images=tuple(output_images),
        input_images=tuple(input_images),
        source_render=source,
        output_render=output,
        engines=engines.versions(),
    )


__all__ = [
    "ChangeMask", "EmbeddedImage", "Frame", "Layer", "Observations", "Reading",
    "Survivability",
    "UnreadablePdf", "probe",
]
