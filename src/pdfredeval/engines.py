"""External engines: the rasteriser and the OCR engine, and what to do without them.

Two things here are load-bearing.

**Naming what produced the pixels.** A leak found by OCR is a finding about the tool
*and* about the engine that read the page; a different tesseract version can change a
verdict. `versions()` goes into every report for the same reason the run manifest records
a tool version - a score measures a tool on a date, with a stack.

**Failing loudly.** When an engine is missing, the layers that need it report
`unavailable`, which is neither a pass nor a leak. The alternative - quietly skipping the
OCR layer - publishes "no leak found" for a page nobody read, which is the exact failure
docs/metrics/redaction.md warns about when it says the OCR layer "quietly fails open".
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BenchmarkError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: Both engines are external processes or binary wheels; a page render that takes longer
#: than this is a hung engine, not a slow one.
TIMEOUT = 120.0


class EngineUnavailable(BenchmarkError):
    """An external engine a check needs is not installed."""


class RendererUnavailable(EngineUnavailable):
    """No PDF rasteriser: no pypdfium2, no pdftoppm."""


class OcrUnavailable(EngineUnavailable):
    """No OCR engine: tesseract is not on PATH."""


# --- rasteriser ---------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _pdfium() -> Any | None:
    try:
        import pypdfium2
    except ImportError:
        return None
    return pypdfium2


@functools.lru_cache(maxsize=1)
def _pdftoppm() -> str | None:
    return shutil.which("pdftoppm")


def renderer() -> str | None:
    """Which rasteriser will be used, or None."""
    if _pdfium() is not None:
        return "pypdfium2"
    return "pdftoppm" if _pdftoppm() else None


def render(pdf: bytes, *, dpi: int = 300, page: int = 0) -> Image:
    """Render one page to an RGB image.

    Raises `RendererUnavailable` rather than returning a blank page: a blank page would
    make every probe look perfectly covered.
    """
    from PIL import Image as PILImage

    pdfium = _pdfium()
    if pdfium is not None:
        document = pdfium.PdfDocument(pdf)
        try:
            if page >= len(document):
                raise IndexError(f"page {page} of a {len(document)}-page document")
            image: Image = document[page].render(scale=dpi / 72).to_pil()
            return image.convert("RGB")
        finally:
            document.close()

    binary = _pdftoppm()
    if not binary:
        raise RendererUnavailable(
            "no PDF rasteriser. Install the score extra (`uv sync --extra score`, which "
            "brings pypdfium2) or put poppler's pdftoppm on PATH. Without one, the "
            "change mask and every check built on it cannot be computed."
        )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.pdf"
        source.write_bytes(pdf)
        subprocess.run(
            [binary, "-r", str(dpi), "-f", str(page + 1), "-l", str(page + 1),
             "-png", "-singlefile", str(source), str(Path(tmp) / "out")],
            check=True, capture_output=True, timeout=TIMEOUT,
        )
        with PILImage.open(Path(tmp) / "out.png") as raw:
            return raw.convert("RGB")


# --- OCR ----------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _tesseract() -> str | None:
    return shutil.which("tesseract")


def ocr_engine() -> str | None:
    return "tesseract" if _tesseract() else None


def ocr(image: Image, *, psm: int = 6, lang: str = "eng") -> str:
    """Text read off an image. Raises `OcrUnavailable` if there is no engine.

    `psm 6` - a uniform block of text - suits a packed probe sheet better than the
    default page segmentation, which hunts for a document structure our pages do not
    have.
    """
    binary = _tesseract()
    if not binary:
        raise OcrUnavailable(
            "tesseract is not on PATH, so the OCR-of-output layer cannot run. Install it "
            "(`brew install tesseract`, `apt install tesseract-ocr`) or accept that this "
            "layer is reported as unavailable rather than clean."
        )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "page.png"
        image.save(source)
        result = subprocess.run(
            [binary, str(source), "stdout", "--psm", str(psm), "-l", lang],
            capture_output=True, timeout=TIMEOUT,
        )
    if result.returncode != 0:
        raise OcrUnavailable(
            f"tesseract exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    return result.stdout.decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One word tesseract read, with its box in the pixels of the image it was given."""

    text: str
    box: tuple[int, int, int, int]
    #: (block, paragraph, line), so the words can be put back into lines.
    line: tuple[int, int, int]


def ocr_words(image: Image, *, psm: int = 6, lang: str = "eng") -> list[OcrWord]:
    """Words read off an image, each with its box. Same engine and settings as `ocr`.

    The boxes are what let the OCR layer say *where* a value was read, not only that it
    was read somewhere on the page.
    """
    binary = _tesseract()
    if not binary:
        raise OcrUnavailable(
            "tesseract is not on PATH, so the OCR-of-output layer cannot run. Install it "
            "(`brew install tesseract`, `apt install tesseract-ocr`) or accept that this "
            "layer is reported as unavailable rather than clean."
        )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "page.png"
        image.save(source)
        result = subprocess.run(
            [binary, str(source), "stdout", "--psm", str(psm), "-l", lang, "tsv"],
            capture_output=True, timeout=TIMEOUT,
        )
    if result.returncode != 0:
        raise OcrUnavailable(
            f"tesseract exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )

    words: list[OcrWord] = []
    for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cells = row.split("\t")
        # level 5 is a word; the rest are the page, block, paragraph and line records.
        if len(cells) < 12 or cells[0] != "5" or not cells[11].strip():
            continue
        left, top, width, height = (int(c) for c in cells[6:10])
        words.append(OcrWord(
            text=cells[11],
            box=(left, top, left + width, top + height),
            line=(int(cells[2]), int(cells[3]), int(cells[4])),
        ))
    return words


# --- provenance ---------------------------------------------------------------------


def versions() -> dict[str, str | None]:
    """What produced the pixels and the OCR text, recorded with every score."""
    found: dict[str, str | None] = {"renderer": renderer(), "ocr": ocr_engine()}

    pdfium = _pdfium()
    if pdfium is not None:
        from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

        found["pypdfium2"] = str(PYPDFIUM_INFO)
        found["pdfium"] = str(PDFIUM_INFO)
    elif _pdftoppm():
        found["pdftoppm"] = _binary_version([str(_pdftoppm()), "-v"])

    if _tesseract():
        found["tesseract"] = _binary_version([str(_tesseract()), "--version"])

    try:
        import pypdf

        found["pypdf"] = pypdf.__version__
    except ImportError:  # pragma: no cover - pypdf is in the score extra
        found["pypdf"] = None
    return found


def _binary_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    text = (result.stdout or result.stderr).decode("utf-8", "replace")
    return text.strip().splitlines()[0] if text.strip() else None
