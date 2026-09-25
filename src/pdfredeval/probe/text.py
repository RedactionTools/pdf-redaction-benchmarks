"""Text-bearing layers of an output PDF: stream, annotations, hidden layers, files.

The canonical real-world redaction failure is a black rectangle over text that is still
in the content stream: it looks redacted, it prints redacted, and `pdftotext` returns the
name. These are the surfaces that catch it - and they are kept *apart* rather than
concatenated, because "the value survived" is a finding and "the value survived in the
annotation you forgot to clear" is an actionable one.
"""

from __future__ import annotations

import io
import re
from typing import Any

from .. import engines
from ..errors import BenchmarkError
from .base import Layer, Reading, Span, unavailable


class UnreadablePdf(BenchmarkError):
    """The output does not parse as a PDF at all."""


def reader(pdf: bytes, *, strict: bool = False) -> Any:
    """A `pypdf` reader over raw bytes, never over a re-saved file."""
    import pypdf

    try:
        return pypdf.PdfReader(io.BytesIO(pdf), strict=strict)
    except Exception as exc:  # pypdf raises a wide family on malformed input
        raise UnreadablePdf(f"output does not parse as a PDF: {exc}") from exc


def page_text(document: Any) -> tuple[str, str]:
    """Content-stream text, split into (visible-stream, optional-content).

    Optional content is separated at extraction time rather than after the fact. Text
    inside an OCG that is off by default is invisible in every viewer and present in
    every parser, so pooling it with the rest would attribute the leak to the wrong
    surface and make the hidden-layer test look like it never catches anything.
    """
    plain: list[str] = []
    hidden: list[str] = []
    depth = [0]

    def before(operator: Any, operands: Any, cm: Any, tm: Any) -> None:
        if operator == b"BDC":
            tag = str(operands[0]) if operands else ""
            if depth[0] or tag in ("/OC", "OC"):
                depth[0] += 1
        elif operator == b"BMC" and depth[0]:
            depth[0] += 1
        elif operator == b"EMC" and depth[0]:
            depth[0] -= 1

    def collect(text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:
        (hidden if depth[0] else plain).append(text)

    for page in document.pages:
        depth[0] = 0
        try:
            page.extract_text(visitor_operand_before=before, visitor_text=collect)
        except Exception:  # noqa: BLE001 - a broken page must not lose the other layers
            continue
    return ("".join(plain), "".join(hidden))


def annotation_text(document: Any) -> str:
    """Widget values, `/Contents`, tooltips and rich text, across every page.

    Annotation text is not in the page content stream, so a tool that rewrites the
    stream and leaves `/Annots` alone passes every visual check and leaks in any viewer
    that renders comments or form fields.
    """
    wanted = ("/Contents", "/V", "/TU", "/RC", "/DV", "/Subj", "/Alt", "/TM")
    found: list[str] = []
    for page in document.pages:
        for reference in page.get("/Annots") or ():
            try:
                annotation = reference.get_object()
            except Exception:  # noqa: BLE001 - a dangling reference is not fatal
                continue
            for key in wanted:
                value = annotation.get(key)
                if value is None:
                    continue
                try:
                    found.append(str(value.get_object() if hasattr(value, "get_object")
                                     else value))
                except Exception:  # noqa: BLE001
                    continue
    return "\n".join(found)


def attachment_text(document: Any) -> str:
    """Text inside embedded files. A PDF attachment is recursed into once."""
    found: list[str] = []
    try:
        attachments = document.attachments
    except Exception:  # noqa: BLE001 - absent or malformed name tree
        return ""
    for name, payloads in (attachments or {}).items():
        for payload in payloads:
            found.append(str(name))
            if payload[:5] == b"%PDF-":
                try:
                    inner = reader(payload)
                    plain, hidden = page_text(inner)
                    found.extend([plain, hidden, annotation_text(inner)])
                except BenchmarkError:
                    continue
            else:
                found.append(payload.decode("utf-8", "replace"))
    return "\n".join(found)


def font_subset_text(document: Any) -> str:
    """Characters an embedded font subset can draw but no visible run uses.

    A subset is built from the glyphs a document actually needed. Remove the text and
    leave the font, and the subset still names its alphabet - which narrows the value
    without disclosing it, hence the layer's low severity. What is returned is the
    residue: the subset's characters with everything currently on the page removed.
    """
    alphabet: set[str] = set()
    for page in document.pages:
        fonts = (page.get("/Resources") or {}).get("/Font") or {}
        try:
            entries = list(fonts.items())
        except Exception:  # noqa: BLE001
            continue
        for _, reference in entries:
            try:
                font = reference.get_object()
                to_unicode = font.get("/ToUnicode")
                if to_unicode is None:
                    continue
                cmap = to_unicode.get_object().get_data().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            for match in re.finditer(r"<([0-9A-Fa-f]{4,})>\s*<([0-9A-Fa-f]{4,})>", cmap):
                raw = match.group(2)
                try:
                    alphabet.update(bytes.fromhex(raw).decode("utf-16-be", "ignore"))
                except ValueError:  # pragma: no cover - malformed cmap entry
                    continue
    return "".join(sorted(alphabet))


def _marked_optional(obj: Any) -> bool:
    """Whether a page object sits inside optional content - a `/OC` marked section."""
    import ctypes

    import pypdfium2.raw as raw

    for k in range(max(0, raw.FPDFPageObj_CountMarks(obj))):
        mark = raw.FPDFPageObj_GetMark(obj, k)
        needed = ctypes.c_ulong()
        if not mark or not raw.FPDFPageObjMark_GetName(mark, None, 0, ctypes.byref(needed)):
            continue
        buffer = ctypes.create_string_buffer(needed.value)
        raw.FPDFPageObjMark_GetName(mark, buffer, needed.value, ctypes.byref(needed))
        if buffer.raw[: needed.value].decode("utf-16-le", "ignore").rstrip("\x00") == "OC":
            return True
    return False


def located_text(
    pdf: bytes, size: tuple[int, int], *, page: int = 0
) -> tuple[list[Span], list[Span]] | None:
    """Every character of one page, boxed in the pixels of a `size` render of it.

    Split the way `page_text` splits: (visible stream, optional content). Mapped through
    pdfium's own page-to-device transform, so a `/Rotate` or an offset media box lands
    where the render put it. A character whose box is empty - invisible text, which
    carries no glyph extent - is placed at its origin. `None` without pdfium.
    """
    import ctypes

    pdfium = engines._pdfium()
    if pdfium is None:
        return None
    import pypdfium2.raw as raw

    visible: list[Span] = []
    hidden: list[Span] = []
    document = pdfium.PdfDocument(pdf)
    try:
        if len(document) != 1:
            # The raster layers read page 0 only; text from pages nobody placed would
            # silently drop out of every haystack. Judge the whole document instead.
            return None
        handle = document[page]
        textpage = handle.get_textpage()
        text = textpage.get_text_range()
        dx, dy = ctypes.c_int(), ctypes.c_int()

        def device(x: float, y: float) -> tuple[float, float]:
            raw.FPDF_PageToDevice(handle, 0, 0, size[0], size[1], 0, x, y,
                                  ctypes.byref(dx), ctypes.byref(dy))
            return (float(dx.value), float(dy.value))

        last: tuple[list[Span], Span] | None = None
        for i in range(min(textpage.count_chars(), len(text))):
            if text[i].isspace():
                # Generated breaks carry no box. Kept, on the box of the character
                # before, so words stay words when a probe's own text is cut out.
                if last is not None:
                    last[0].append(Span(" ", last[1].box))
                continue
            left, bottom, right, top = textpage.get_charbox(i, loose=True)
            if right <= left or top <= bottom:
                ox, oy = ctypes.c_double(), ctypes.c_double()
                raw.FPDFText_GetCharOrigin(textpage, i, ctypes.byref(ox), ctypes.byref(oy))
                left = right = ox.value
                bottom = top = oy.value
            corners = [device(left, bottom), device(right, top)]
            xs, ys = [c[0] for c in corners], [c[1] for c in corners]
            span = Span(text[i], (min(xs), min(ys), max(xs), max(ys)))
            owner = textpage.get_textobj(i)
            bucket = hidden if owner is not None and _marked_optional(owner) else visible
            bucket.append(span)
            last = (bucket, span)
    except Exception:  # noqa: BLE001 - no positions is a fallback, not a failure
        return None
    finally:
        document.close()
    return (visible, hidden)


def read(pdf: bytes) -> list[Reading]:
    """Every text-bearing structural layer of one output, as separate readings."""
    try:
        document = reader(pdf)
    except UnreadablePdf as exc:
        return [
            unavailable(layer, str(exc))
            for layer in (Layer.CONTENT_STREAM, Layer.OPTIONAL_CONTENT,
                          Layer.ANNOTATION, Layer.ATTACHMENT, Layer.FONT_SUBSET)
        ]

    plain, hidden = page_text(document)
    visible = plain
    residue = "".join(sorted(set(font_subset_text(document)) - set(visible)))
    return [
        Reading(Layer.CONTENT_STREAM, plain),
        Reading(Layer.OPTIONAL_CONTENT, hidden),
        Reading(Layer.ANNOTATION, annotation_text(document)),
        Reading(Layer.ATTACHMENT, attachment_text(document)),
        Reading(Layer.FONT_SUBSET, residue,
                detail={"alphabet": len(residue)}),
    ]
