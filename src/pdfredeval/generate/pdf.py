"""A minimal PDF writer, deliberately low-level.

A high-level library hides precisely what this benchmark needs to control: text render
mode 3, values reachable only from an annotation, a custom Info key, an earlier
incremental-update revision still holding the original. So objects, streams and the xref
are built here by hand, with no dependencies.

Classic cross-reference tables rather than xref streams: a generated case should be
readable in a text editor when a result is disputed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

A4 = (595.28, 841.89)


@dataclass(frozen=True, slots=True)
class Name:
    """A PDF name object, serialized `/value`."""

    value: str

    def __str__(self) -> str:
        return f"/{self.value}"


@dataclass(frozen=True, slots=True)
class Ref:
    """An indirect reference, serialized `n 0 R`."""

    num: int

    def __str__(self) -> str:
        return f"{self.num} 0 R"


@dataclass(frozen=True, slots=True)
class Raw:
    """Pre-serialized bytes, passed through untouched."""

    text: str


@dataclass(slots=True)
class Stream:
    dictionary: dict[str, Any]
    data: bytes


#: WinAnsiEncoding is cp1252. The base-14 fonts this writer uses declare it, so a show
#: string must be encoded that way - one byte per glyph.
WINANSI = "cp1252"


def _escape_bytes(raw: bytes) -> bytes:
    for old, new in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)"),
                     (b"\r", b"\\r"), (b"\n", b"\\n")):
        raw = raw.replace(old, new)
    return b"(" + raw + b")"


def escape_show_string(text: str) -> bytes:
    """Serialize text for a content-stream show operator.

    Distinct from `escape_string`, which serializes a *document* string (a title, an
    annotation's contents) where UTF-16 is allowed. A show string is indexed through the
    font's encoding, so UTF-16 there would make a simple font draw a notdef between every
    character - text that is present in the file and illegible on the page.
    """
    try:
        raw = text.encode(WINANSI)
    except UnicodeEncodeError as exc:
        bad = text[exc.start : exc.start + 1]
        raise ValueError(
            f"cannot show {text!r} with a WinAnsiEncoding font: {bad!r} is outside "
            f"cp1252. Non-Latin script needs an embedded composite font, which this "
            f"writer does not build yet."
        ) from None
    return _escape_bytes(raw)


def escape_string(text: str) -> bytes:
    r"""Serialize a text string.

    ASCII becomes a literal `(...)` string; anything else becomes a UTF-16BE hex string,
    which is how a PDF carries non-Latin text that extraction must still recover.
    """
    if all(ord(c) < 128 for c in text):
        return _escape_bytes(text.encode("ascii"))
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def serialize(value: Any) -> bytes:
    if isinstance(value, Raw):
        return value.text.encode("latin-1")
    if isinstance(value, (Name, Ref)):
        return str(value).encode("latin-1")
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, float):
        # Trim trailing zeros: PDF has no notion of significant figures, and short
        # numbers keep generated files diffable.
        return f"{value:.4f}".rstrip("0").rstrip(".").encode("ascii").replace(b"-0", b"0")
    if isinstance(value, str):
        return escape_string(value)
    if isinstance(value, bytes):
        return value
    if isinstance(value, (list, tuple)):
        return b"[" + b" ".join(serialize(v) for v in value) + b"]"
    if isinstance(value, dict):
        parts = [b"<<"]
        for key, val in value.items():
            parts.append(b"/" + key.encode("latin-1") + b" " + serialize(val))
        parts.append(b">>")
        return b" ".join(parts)
    raise TypeError(f"cannot serialize {type(value).__name__}")


class PdfWriter:
    """Accumulates objects, then emits a file with a classic xref table."""

    def __init__(self, *, version: str = "1.7") -> None:
        self.version = version
        self._objects: dict[int, Any] = {}
        self._next = 1
        self.trailer: dict[str, Any] = {}

    def reserve(self) -> Ref:
        """Allocate an object number before its contents exist (for circular refs)."""
        ref = Ref(self._next)
        self._next += 1
        self._objects[ref.num] = None
        return ref

    def put(self, ref: Ref, value: Any) -> Ref:
        self._objects[ref.num] = value
        return ref

    def add(self, value: Any) -> Ref:
        return self.put(self.reserve(), value)

    def add_stream(self, dictionary: dict[str, Any], data: bytes) -> Ref:
        return self.add(Stream(dictionary, data))

    # --- emit ---------------------------------------------------------------------

    def _body(self, nums: Sequence[int], start_offset: int) -> tuple[bytes, dict[int, int]]:
        out = bytearray()
        offsets: dict[int, int] = {}
        for num in nums:
            value = self._objects[num]
            if value is None:
                raise ValueError(f"object {num} was reserved but never filled")
            offsets[num] = start_offset + len(out)
            out += f"{num} 0 obj\n".encode("ascii")
            if isinstance(value, Stream):
                d = dict(value.dictionary)
                d["Length"] = len(value.data)
                out += serialize(d) + b"\nstream\n" + value.data + b"\nendstream"
            else:
                out += serialize(value)
            out += b"\nendobj\n"
        return bytes(out), offsets

    @staticmethod
    def _xref_section(offsets: dict[int, int]) -> bytes:
        """Classic xref, grouped into contiguous subsections."""
        nums = sorted(offsets)
        out = bytearray(b"xref\n")
        i = 0
        while i < len(nums):
            j = i
            while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
                j += 1
            out += f"{nums[i]} {j - i + 1}\n".encode("ascii")
            for num in nums[i : j + 1]:
                out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
            i = j + 1
        return bytes(out)

    def build(self) -> bytes:
        header = f"%PDF-{self.version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1")
        body, offsets = self._body(sorted(self._objects), len(header))
        xref_offset = len(header) + len(body)
        trailer = dict(self.trailer)
        trailer["Size"] = max(offsets) + 1
        return (
            header
            + body
            + self._xref_section(offsets)
            + b"trailer\n"
            + serialize(trailer)
            + f"\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )

    def build_with_revision(self, updated: dict[int, Any]) -> bytes:
        """Emit the file, then append an incremental update replacing `updated` objects.

        The first revision stays in the bytes, reachable through the previous xref. This
        is how a case plants a value in an *earlier revision* of its own input - the
        failure mode of a tool that only processes the current revision.
        """
        base = self.build()
        prev_xref = int(re.findall(rb"startxref\s+(\d+)\s+%%EOF\s*$", base)[0])
        for num, value in updated.items():
            self._objects[num] = value
        body, offsets = self._body(sorted(updated), len(base))
        xref_offset = len(base) + len(body)
        trailer = dict(self.trailer)
        trailer["Size"] = max(self._objects) + 1
        trailer["Prev"] = prev_xref
        return (
            base
            + body
            + self._xref_section(offsets)
            + b"trailer\n"
            + serialize(trailer)
            + f"\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
        )


# --- geometry -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Matrix:
    """A PDF transformation matrix [a b c d e f]."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @classmethod
    def translate(cls, x: float, y: float) -> Matrix:
        return cls(e=x, f=y)

    @classmethod
    def rotate(cls, degrees: float) -> Matrix:
        r = math.radians(degrees)
        return cls(a=math.cos(r), b=math.sin(r), c=-math.sin(r), d=math.cos(r))

    def then(self, other: Matrix) -> Matrix:
        """self applied first, then other."""
        return Matrix(
            a=self.a * other.a + self.b * other.c,
            b=self.a * other.b + self.b * other.d,
            c=self.c * other.a + self.d * other.c,
            d=self.c * other.b + self.d * other.d,
            e=self.e * other.a + self.f * other.c + other.e,
            f=self.e * other.b + self.f * other.d + other.f,
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def as_operands(self) -> str:
        vals = (self.a, self.b, self.c, self.d, self.e, self.f)
        return " ".join(f"{v:.5f}".rstrip("0").rstrip(".") for v in vals)


def aabb(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points, strict=True)
    return (min(xs), min(ys), max(xs), max(ys))


# --- fonts ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Font:
    """A base-14 font. No embedding, no width tables to ship.

    Courier is the default because it is metrically exact at 600/1000 em per glyph, which
    makes every ground-truth bbox exact. Coverage metrics compare a box against a change
    mask, so an approximate box would quietly corrupt the scores it feeds. Proportional
    fonts need an AFM width table before their boxes can be trusted.
    """

    resource: str
    base_font: str
    char_width: float = 0.6
    ascent: float = 0.70
    descent: float = 0.20
    exact_metrics: bool = True

    def width(self, text: str, size: float) -> float:
        if not self.exact_metrics:
            raise ValueError(
                f"{self.base_font} has no width table; its bboxes would be approximate. "
                f"Use COURIER, or add an AFM table for it."
            )
        return len(text) * self.char_width * size

    def dictionary(self) -> dict[str, Any]:
        return {
            "Type": Name("Font"),
            "Subtype": Name("Type1"),
            "BaseFont": Name(self.base_font),
            "Encoding": Name("WinAnsiEncoding"),
        }


COURIER = Font("F1", "Courier")
COURIER_BOLD = Font("F2", "Courier-Bold")
FONTS = (COURIER, COURIER_BOLD)


# --- self-check -----------------------------------------------------------------------


def check_xref(pdf: bytes) -> list[str]:
    """Verify every xref entry points at the object header it claims.

    A one-byte offset error produces a file that some viewers open and others reject,
    which would show up as an unexplained tool failure rather than as our bug. The
    generator runs this on every case before writing it.
    """
    problems: list[str] = []
    starts = [int(m) for m in re.findall(rb"startxref\s+(\d+)", pdf)]
    if not starts:
        return ["no startxref"]
    seen: set[int] = set()
    queue = [starts[-1]]
    while queue:
        offset = queue.pop()
        if offset in seen:
            continue
        seen.add(offset)
        if not pdf[offset : offset + 4] == b"xref":
            problems.append(f"offset {offset} does not begin an xref table")
            continue
        section = pdf[offset:]
        end = section.find(b"trailer")
        if end < 0:
            problems.append(f"xref at {offset} has no trailer")
            continue
        lines = section[4:end].split(b"\n")
        expect_num: int | None = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            head = line.split()
            if len(head) == 2 and not line.endswith(b"n") and b"n" not in line:
                expect_num = int(head[0])
                continue
            if len(head) >= 3 and head[2] in (b"n", b"f"):
                if expect_num is None:
                    continue
                if head[2] == b"n":
                    target = int(head[0])
                    want = f"{expect_num} 0 obj".encode("ascii")
                    if not pdf[target : target + len(want)] == want:
                        got = pdf[target : target + 24]
                        problems.append(
                            f"object {expect_num}: xref says {target}, found {got!r}"
                        )
                expect_num += 1
        prev = re.search(rb"/Prev\s+(\d+)", section[:end])
        if prev:
            queue.append(int(prev.group(1)))
    return problems
