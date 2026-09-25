"""Synthetic redaction tools, with known answers.

No real vendor ships in-tree, so the scorer is tested against tools whose behaviour we
choose. Each of these is a failure mode the benchmark exists to tell apart, and each has
a score it must produce:

| fake | what it does | must score |
|---|---|---|
| `do_nothing` | returns the input | every target leaks |
| `black_box` | paints over the pixels, leaves the text | pixels clean, content stream leaks |
| `true_redaction` | removes the values everywhere | no leaks, document intact |
| `rasterise` | flattens the page to an image | no leaks, and the document destroyed |
| `rotate` / `rescale` | a turned or resized page | the aligner recovers the mapping |
| `crop_corner` | loses a registration mark | alignment degraded, raster layers unavailable |

They are built with `pypdf` the way a real tool would rebuild a file - which is also why
the prior revision disappears from all of them but `do_nothing`.
"""

from __future__ import annotations

import io
import zlib
from typing import Any

import pypdf
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pdfredeval.generate.pdf import Name, PdfWriter, escape_show_string
from pdfredeval.types import Case, Probe

BLACK = b"0 g"


def _writer(case: Case) -> Any:
    return pypdf.PdfWriter(clone_from=io.BytesIO(case.pdf_bytes))


def _emit(writer: Any) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _content(page: Any) -> bytes:
    return bytes(page.get_contents().get_data())


def _set_content(page: Any, data: bytes) -> None:
    stream = DecodedStreamObject()
    stream.set_data(data)
    page.replace_contents(stream)


def _cover_ops(probes: tuple[Probe, ...], pad: float = 1.0) -> bytes:
    """Filled black rectangles over each probe's box - the classic fake redaction."""
    ops = [b"q", BLACK]
    for probe in probes:
        if probe.bbox is None:
            continue
        box = probe.bbox
        ops.append(
            f"{box.x0 - pad:.3f} {box.y0 - pad:.3f} "
            f"{box.x1 - box.x0 + 2 * pad:.3f} {box.y1 - box.y0 + 2 * pad:.3f} re f"
            .encode()
        )
    ops.append(b"Q")
    return b"\n".join(ops) + b"\n"


# --- the fakes -------------------------------------------------------------------------


def do_nothing(case: Case) -> bytes:
    """The input, unchanged. Every target must leak; nothing may be over-redacted."""
    return case.pdf_bytes


def black_box(case: Case, probes: tuple[Probe, ...] | None = None) -> bytes:
    """A black rectangle over every target, with the text left in the content stream.

    The canonical real-world failure: it looks redacted, it prints redacted, and
    `pdftotext` returns the name.
    """
    writer = _writer(case)
    page = writer.pages[0]
    _set_content(page, _content(page) + _cover_ops(probes or case.targets))
    return _emit(writer)


def true_redaction(case: Case, *, survivors: int = 0) -> bytes:
    """Values removed from every surface, and the page covered where they were.

    What a correct tool does: the glyphs go, the annotation values go, the hidden layer
    goes, the metadata goes, and the pixels are covered - while everything else on the
    page survives.

    `survivors` leaves the first N drawn copies of each value in the stream, under their
    covers: one missed instance on a page that repeats a value.
    """
    writer = _writer(case)
    page = writer.pages[0]

    content = _content(page)
    for probe in case.probes:
        if not (probe.must_redact and probe.value):
            continue
        literal = escape_show_string(probe.value) + b" Tj"
        kept, _, rest = _split_after(content, literal, survivors)
        content = kept + rest.replace(literal, b"() Tj")
        # Stacked runs are drawn one glyph per operator, so the value never appears as
        # a contiguous literal; each glyph has to be cleared on its own.
        if probe.conditions.orientation == "vertical":
            for char in probe.value:
                content = content.replace(escape_show_string(char) + b" Tj", b"() Tj")
    _set_content(page, content + _cover_ops(case.targets))

    if NameObject("/Annots") in page:
        del page[NameObject("/Annots")]
    _strip_metadata(writer)
    return _emit(writer)


def _split_after(data: bytes, needle: bytes, count: int) -> tuple[bytes, bytes, bytes]:
    """(everything through the `count`-th `needle`, "", the rest)."""
    end = 0
    for _ in range(count):
        found = data.find(needle, end)
        if found < 0:
            break
        end = found + len(needle)
    return data[:end], b"", data[end:]


def _strip_metadata(writer: Any) -> None:
    """Clear the Info dictionary and the XMP packet, as an exporting tool would."""
    writer.add_metadata({})
    info = writer._info  # noqa: SLF001 - pypdf exposes no public "replace Info"
    if isinstance(info, DictionaryObject):
        for key in list(info.keys()):
            del info[key]
    root = writer.root_object
    if NameObject("/Metadata") in root:
        del root[NameObject("/Metadata")]


def rasterise(case: Case, *, dpi: int = 150, rotate: int = 0, scale: float = 1.0,
              white_corner: bool = False, footer: bool = False) -> bytes:
    """The blunt instrument: the page flattened to a single image.

    Redacts everything perfectly and destroys the document, which is exactly why
    `text_retention` and the not-rasterised gate exist. Also the vehicle for the
    geometry fakes - a turned, resized or cropped page is this with one argument set.
    """
    from PIL import Image

    from pdfredeval import engines

    image = engines.render(case.pdf_bytes, dpi=dpi)
    if scale != 1.0:
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    if rotate:
        image = image.rotate(-rotate, expand=True, fillcolor=(255, 255, 255))
    if white_corner:
        # Paint out one registration mark, as a tool that crops or stamps a margin
        # would. The aligner must notice and decline rather than guess.
        patch = Image.new("RGB", (image.width // 6, image.height // 10), (255, 255, 255))
        image.paste(patch, (0, 0))
    if footer:
        # A free tier's "Redacted with ..." bar across the foot of the page: it hides
        # both bottom marks, the donut among them, and nothing else moves.
        bar = Image.new("RGB", (image.width, image.height // 16), (40, 44, 52))
        image.paste(bar, (0, image.height - bar.height))

    page_size = case.page_size or (595.28, 841.89)
    if rotate in (90, 270):
        page_size = (page_size[1], page_size[0])
    return _image_pdf(image, page_size)


def _image_pdf(image: Any, page_size: tuple[float, float]) -> bytes:
    """A one-page PDF whose only content is this bitmap, filling the page."""
    writer = PdfWriter()
    samples = image.convert("RGB").tobytes()
    picture = writer.add_stream(
        {
            "Type": Name("XObject"), "Subtype": Name("Image"),
            "Width": image.width, "Height": image.height,
            "ColorSpace": Name("DeviceRGB"), "BitsPerComponent": 8,
            "Filter": Name("FlateDecode"),
        },
        zlib.compress(samples, 6),
    )
    width, height = page_size
    content = writer.add_stream(
        {}, f"q {width:.4f} 0 0 {height:.4f} 0 0 cm /Im0 Do Q".encode()
    )
    pages = writer.reserve()
    page = writer.add({
        "Type": Name("Page"), "Parent": pages,
        "MediaBox": [0, 0, round(width, 4), round(height, 4)],
        "Resources": {"XObject": {"Im0": picture}},
        "Contents": content,
    })
    writer.put(pages, {"Type": Name("Pages"), "Kids": [page], "Count": 1})
    catalog = writer.add({"Type": Name("Catalog"), "Pages": pages})
    writer.trailer = {"Root": catalog}
    return writer.build()


def rotate(case: Case, degrees: int = 90, *, dpi: int = 150) -> bytes:
    """A quarter-turned page, as a rescan produces."""
    return rasterise(case, dpi=dpi, rotate=degrees)


def rescale(case: Case, factor: float = 0.75, *, dpi: int = 150) -> bytes:
    """A resized page, as a tool that normalises to Letter produces."""
    return rasterise(case, dpi=dpi, scale=factor)


def crop_corner(case: Case, *, dpi: int = 150) -> bytes:
    """A page that lost a registration mark."""
    return rasterise(case, dpi=dpi, white_corner=True)


def footer_stamp(case: Case, *, dpi: int = 150, scale: float = 1.0) -> bytes:
    """A rescaled page with a vendor's bar stamped over its foot, both bottom marks gone."""
    return rasterise(case, dpi=dpi, scale=scale, footer=True)


def broken_pdf(case: Case) -> bytes:
    """Bytes that are not a PDF. Every gate must fail rather than raise."""
    return b"%PDF-1.7\nthis is not a pdf\n"


__all__ = [
    "black_box", "broken_pdf", "crop_corner", "do_nothing", "footer_stamp", "rasterise",
    "rescale", "rotate", "true_redaction",
]
