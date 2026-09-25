"""Metadata: Info dict, XMP, custom keys, document id, piece info.

The value here is invisible in every viewer and survives forwarding, which is exactly why
it is worth checking and exactly why tools forget it. docs/metrics/extraction.md builds a
whole channel on it: a metadata-only probe that survives proves the tool never opened the
channel at all.

Ground truth records *that* a probe lives in metadata, never under which key - so the
test is "does this value appear in any metadata value anywhere", not a lookup. That also
makes the check honest about a tool that moves a value between keys instead of removing
it.
"""

from __future__ import annotations

import re
from typing import Any

from .base import Layer, Reading, unavailable
from .text import UnreadablePdf, reader

#: XMP text content, so a value is found whether it sits in an element or an attribute.
_TAG = re.compile(r"<[^>]*>")

#: Where a PDF hides strings that are not the page: application-private data, the
#: document identity, the marked-content properties tools leave behind.
_NESTED_KEYS = ("/PieceInfo", "/Private", "/AF", "/Properties")


def info_values(document: Any) -> list[tuple[str, str]]:
    try:
        info = document.metadata or {}
    except Exception:  # noqa: BLE001 - a malformed Info dict is not fatal
        return []
    return [(str(k), str(v)) for k, v in dict(info).items() if v is not None]


def xmp_text(document: Any) -> str:
    """The XMP packet as plain text, tags stripped."""
    try:
        stream = (document.root_object or {}).get("/Metadata")
        if stream is None:
            return ""
        raw = stream.get_object().get_data()
    except Exception:  # noqa: BLE001
        return ""
    return _TAG.sub(" ", raw.decode("utf-8", "replace"))


def document_id(document: Any) -> list[str]:
    """`/ID`, which several tools derive from the original file's contents."""
    try:
        identifiers = (document.trailer or {}).get("/ID") or ()
        return [str(part) for part in identifiers]
    except Exception:  # noqa: BLE001
        return []


def piece_info(document: Any) -> list[str]:
    """Strings under the catalog's and pages' application-private dictionaries."""
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            items = node.items()
        except Exception:  # noqa: BLE001 - not a dictionary
            return
        for key, value in items:
            try:
                value = value.get_object()
            except Exception:  # noqa: BLE001
                continue
            if hasattr(value, "items"):
                walk(value, depth + 1)
            elif isinstance(value, (str, bytes)):
                found.append(value.decode("utf-8", "replace")
                             if isinstance(value, bytes) else str(value))
            _ = key
    for holder in (document.root_object, *(p for p in document.pages)):
        for key in _NESTED_KEYS:
            try:
                node = (holder or {}).get(key)
            except Exception:  # noqa: BLE001
                continue
            if node is not None:
                walk(node.get_object())
    return found


def read(pdf: bytes) -> Reading:
    """Every metadata value in the output, as one searchable blob."""
    try:
        document = reader(pdf)
    except UnreadablePdf as exc:
        return unavailable(Layer.METADATA, str(exc))

    pairs = info_values(document)
    parts = [f"{key} {value}" for key, value in pairs]
    parts.append(xmp_text(document))
    parts.extend(document_id(document))
    parts.extend(piece_info(document))
    return Reading(
        Layer.METADATA,
        "\n".join(part for part in parts if part),
        detail={"info_keys": sorted(key for key, _ in pairs)},
    )
