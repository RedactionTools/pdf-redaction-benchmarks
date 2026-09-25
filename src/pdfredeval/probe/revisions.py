"""Earlier revisions: what the file looked like before the redaction was saved.

A PDF written by incremental update keeps every previous generation in the same file. A
tool that draws over a name and calls `save` rather than `export` ships the original
inside the "redacted" document, where a text editor recovers it. docs/metrics/redaction.md
rates this critical for that reason.

Each generation is parsed **separately**, by truncating the file at its own `%%EOF` and
handing that prefix to the parser. Walking `/Prev` by hand would tie this to the xref
tables our own writer emits; vendor output uses xref *streams* just as often, and the
truncation trick is indifferent to which.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Layer, Reading, unavailable
from .text import UnreadablePdf, annotation_text, page_text, reader

_EOF = re.compile(rb"%%EOF")

#: A prefix shorter than this cannot be a PDF, and trying to parse it wastes time.
_MIN_PREFIX = 64


def prefixes(pdf: bytes) -> list[bytes]:
    """Every byte prefix that ends at an `%%EOF` except the last - the earlier files."""
    ends = [m.end() for m in _EOF.finditer(pdf)]
    return [pdf[:end] for end in ends[:-1] if end >= _MIN_PREFIX]


def revision_text(prefix: bytes) -> str:
    """Everything readable in one earlier generation."""
    try:
        document = reader(prefix)
        plain, hidden = page_text(document)
        meta = " ".join(str(v) for v in (dict(document.metadata or {})).values())
    except UnreadablePdf:
        return ""
    except Exception:  # noqa: BLE001 - a truncated prefix is allowed to be nonsense
        return ""
    return "\n".join(part for part in (plain, hidden, annotation_text(document), meta)
                     if part)


def read(pdf: bytes) -> Reading:
    """The concatenated contents of every generation before the current one."""
    try:
        reader(pdf)
    except UnreadablePdf as exc:
        # "No earlier generation found" in a file that does not parse is not a finding.
        # Without this the layer reports clean on an unopenable output, and one clean
        # layer is enough to make every probe on the page look redacted.
        return unavailable(Layer.PRIOR_REVISION, str(exc))

    earlier = prefixes(pdf)
    if not earlier:
        return Reading(Layer.PRIOR_REVISION, "", detail={"revisions": 0})

    texts = [revision_text(prefix) for prefix in earlier]
    parsed = sum(1 for t in texts if t)
    detail: dict[str, Any] = {"revisions": len(earlier), "parsed": parsed}
    if parsed == 0:
        # The file says it has earlier generations and none of them could be read. That
        # is not "no leak found"; it is a layer that could not be checked.
        return Reading(
            Layer.PRIOR_REVISION, "",
            unavailable=f"{len(earlier)} earlier generation(s) present, none parsable",
            detail=detail,
        )
    return Reading(Layer.PRIOR_REVISION, "\n".join(texts), detail=detail)
