"""What a prober is, and the layers they report on.

A prober extracts *one observable surface* of an output PDF and hands back what it found.
None of them decides an outcome: that separation is what lets the scorer say which layer
caught a leak, which is the part a vendor can act on (docs/metrics/redaction.md).

Three states, and the third is the reason this file exists. A layer can report that a
value survived, that it did not, or that **the layer could not be read at all** - no OCR
engine, no rasteriser, an output that does not parse. Collapsing `unavailable` into
"clean" is how a benchmark publishes a passing score for a page nobody looked at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..types import Severity


class Layer(str, Enum):
    """Where a redacted value can survive. docs/metrics/redaction.md.

    Ordered worst-first, which is also the order a report reads best in.
    """

    RENDERED_PIXELS = "rendered_pixels"
    CONTENT_STREAM = "content_stream"
    PRIOR_REVISION = "prior_revision"
    IMAGE_XOBJECT = "image_xobject"
    METADATA = "metadata"
    ANNOTATION = "annotation"
    ATTACHMENT = "attachment"
    OPTIONAL_CONTENT = "optional_content"
    OCR = "ocr"
    FONT_SUBSET = "font_subset"

    @property
    def severity(self) -> Severity:
        """What a survival *means* - not how hard the layer is to reach."""
        return _LAYER_SEVERITY[self]

    @property
    def description(self) -> str:
        return _LAYER_DESCRIPTION[self]


_LAYER_SEVERITY = {
    Layer.RENDERED_PIXELS: Severity.CRITICAL,
    Layer.CONTENT_STREAM: Severity.CRITICAL,
    Layer.PRIOR_REVISION: Severity.CRITICAL,
    Layer.IMAGE_XOBJECT: Severity.CRITICAL,
    Layer.METADATA: Severity.HIGH,
    Layer.ANNOTATION: Severity.HIGH,
    Layer.ATTACHMENT: Severity.HIGH,
    Layer.OPTIONAL_CONTENT: Severity.HIGH,
    Layer.OCR: Severity.HIGH,
    Layer.FONT_SUBSET: Severity.LOW,
}

_LAYER_DESCRIPTION = {
    Layer.RENDERED_PIXELS: "visible to anyone who opens the file",
    Layer.CONTENT_STREAM: "copy-paste, pdftotext, any parser",
    Layer.PRIOR_REVISION: "'save' instead of export; trivially recovered",
    Layer.IMAGE_XOBJECT: "a crop is a view, not a deletion",
    Layer.METADATA: "invisible, survives forwarding",
    Layer.ANNOTATION: "shown by many viewers",
    Layer.ATTACHMENT: "one toggle away",
    Layer.OPTIONAL_CONTENT: "one toggle away",
    Layer.OCR: "catches translucent or hairline covers",
    Layer.FONT_SUBSET: "narrows the value; partial disclosure",
}

#: Layers whose test is "does this text survive here?". The rest have their own tests.
TEXT_LAYERS = (
    Layer.CONTENT_STREAM,
    Layer.PRIOR_REVISION,
    Layer.ANNOTATION,
    Layer.ATTACHMENT,
    Layer.OPTIONAL_CONTENT,
    Layer.OCR,
)


@dataclass(frozen=True, slots=True)
class Span:
    """A piece of a layer's text, and where on the page it was drawn.

    `box` is in input-raster pixels once `probe()` has placed it, whatever frame the
    layer read it in. `probes` names every probe whose box holds the span's centre: text
    drawn inside another probe's box is evidence about *that* probe, and on a page that
    repeats one value under many conditions, judging each probe against the whole page
    turns one surviving copy into a leak on every probe.
    """

    text: str
    box: tuple[float, float, float, float]
    probes: frozenset[str] = frozenset()

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


@dataclass(frozen=True, slots=True)
class Reading:
    """One layer's report: the text it yielded, or why it could not be read.

    `spans`, when present, is the same text with positions. `None` means the layer has
    no geometry (metadata, attachments) or it could not be placed on the input, and a
    probe is then judged against the whole of `text`.
    """

    layer: Layer
    text: str = ""
    unavailable: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    spans: tuple[Span, ...] | None = None

    @property
    def readable(self) -> bool:
        return self.unavailable is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "chars": len(self.text),
            "unavailable": self.unavailable,
            **({"detail": self.detail} if self.detail else {}),
            **({"spans": len(self.spans)} if self.spans is not None else {}),
        }


def unavailable(layer: Layer, reason: str) -> Reading:
    return Reading(layer=layer, unavailable=reason)
