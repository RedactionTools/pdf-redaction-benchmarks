"""Embedded images, and whether one that should be gone is still recognisably there.

A crop is a view, not a deletion: an image XObject clipped to hide a face still carries
the face, and any parser can pull the full bitmap back out. So images are compared by
**perceptual hash** rather than by bytes - re-encoding, rescaling and requantising all
change the bytes and none of them change what the picture shows.

docs/metrics/core.md fixes the test: probe `i` leaks if any image in the output satisfies
`Hamming(h_i, h_j) <= tau_hash`, ignoring crop and mask.

A note on today's dataset: the generator plants no image, face, signature or code probes
yet, and records no reference hash. This module therefore derives the reference from the
*input* PDF - the images that overlap a probe's box - so it is correct the day those
probes appear, and reports zero applicable probes until then rather than pretending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from ..types import BBox
from .base import Layer, Reading, unavailable
from .text import UnreadablePdf, reader

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: pHash working size. 32x32 gives a DCT whose low-frequency corner is stable under the
#: rescaling and requantisation a PDF round trip applies.
SIZE = 32
HASH_SIDE = 8


@dataclass(frozen=True, slots=True)
class EmbeddedImage:
    """One image XObject, with where it sits if that could be determined."""

    name: str
    phash: int
    width: int
    height: int
    page: int
    bbox: BBox | None = None

    def distance(self, other: int) -> int:
        return int(bin(self.phash ^ other).count("1"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "phash": f"{self.phash:016x}",
            "width": self.width, "height": self.height, "page": self.page,
        }


def _dct_matrix(n: int) -> Any:
    """DCT-II basis. Built here rather than imported: scipy is not a dependency."""
    k = np.arange(n)
    basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    basis[0] *= np.sqrt(0.5)
    return basis * np.sqrt(2.0 / n)


_BASIS = _dct_matrix(SIZE)


def phash(image: Image) -> int:
    """64-bit DCT perceptual hash.

    The DC term is dropped before taking the median: it carries overall brightness, which
    a re-encode shifts and which says nothing about what the image depicts.
    """
    from PIL import Image as PILImage

    grey = image.convert("L").resize((SIZE, SIZE), PILImage.Resampling.LANCZOS)
    pixels = np.asarray(grey, dtype=float)
    spectrum = _BASIS @ pixels @ _BASIS.T
    low = spectrum[:HASH_SIDE, :HASH_SIDE].flatten()
    median = float(np.median(low[1:]))
    bits = 0
    for index, value in enumerate(low):
        if value > median:
            bits |= 1 << index
    return bits


def hamming(a: int, b: int) -> int:
    return int(bin(a ^ b).count("1"))


def embedded(pdf: bytes) -> list[EmbeddedImage]:
    """Every image XObject in a document, hashed."""
    document = reader(pdf)
    found: list[EmbeddedImage] = []
    for number, page in enumerate(document.pages):
        try:
            images = list(page.images)
        except Exception:  # noqa: BLE001 - an undecodable image is not a parse failure
            continue
        for item in images:
            try:
                picture = item.image
                if picture is None:
                    continue
                found.append(EmbeddedImage(
                    name=str(item.name), phash=phash(picture),
                    width=picture.width, height=picture.height, page=number,
                ))
            except Exception:  # noqa: BLE001 - exotic colour spaces, broken filters
                continue
    return found


def read(pdf: bytes) -> tuple[Reading, list[EmbeddedImage]]:
    """Hashes of every image in the output, for the image-identity test."""
    try:
        images = embedded(pdf)
    except UnreadablePdf as exc:
        return (unavailable(Layer.IMAGE_XOBJECT, str(exc)), [])
    except Exception as exc:  # noqa: BLE001 - defensive: never lose the other layers
        return (unavailable(Layer.IMAGE_XOBJECT, f"images unreadable: {exc}"), [])
    return (
        Reading(Layer.IMAGE_XOBJECT, "",
                detail={"images": [i.to_dict() for i in images]}),
        images,
    )


def reference_hashes(case_pdf: bytes) -> list[EmbeddedImage]:
    """Hashes of the *input's* images, the thing an output image is compared against.

    Ground truth carries no image hash today, so the reference is recovered from the case
    file. That is not a workaround for a missing field: the case PDF is ground truth, and
    hashing it at scoring time is reproducible from the seed like everything else.
    """
    try:
        return embedded(case_pdf)
    except Exception:  # noqa: BLE001 - our own file; a failure here is a bug elsewhere
        return []
