"""Page geometry: fiducials and probe slots.

Fiducials exist because the tool hands back a re-encoded PDF whose coordinates need not
match ours. Three identical corner squares plus one distinct marker make the mapping
recoverable *and* unambiguous under rotation - four identical squares would leave a
180-degree flip undetectable, which is exactly the failure a rescanned page produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..types import Fiducial
from .pdf import A4

FIDUCIAL_SIZE = 8.0
FIDUCIAL_INSET = 18.0

__all__ = [
    "FIDUCIAL_INSET", "FIDUCIAL_SIZE", "Fiducial", "Grid", "OutOfSpace",
    "PageLayout", "RowPacker", "Slot", "Stack",
]


@dataclass(frozen=True, slots=True)
class Slot:
    """A rectangle a probe may be drawn into. Origin bottom-left, PDF points."""

    x: float
    y: float
    width: float
    height: float

    @property
    def baseline(self) -> tuple[float, float]:
        """Where text starts: left edge, a little above the slot floor."""
        return (self.x, self.y + self.height * 0.25)


@dataclass(frozen=True, slots=True)
class PageLayout:
    width: float = A4[0]
    height: float = A4[1]
    margin: float = 54.0
    #: Strip at the head of the page for the provenance banner. Excluded from `content`,
    #: so probes can never be laid over it.
    brand: float = 46.0

    def fiducials(self) -> tuple[Fiducial, ...]:
        i, s = FIDUCIAL_INSET, FIDUCIAL_SIZE
        c = i + s / 2
        return (
            Fiducial("tl", c, self.height - c),
            Fiducial("tr", self.width - c, self.height - c),
            Fiducial("bl", c, c),
            Fiducial("br", self.width - c, c, kind="donut"),
        )

    @property
    def content(self) -> Slot:
        m = self.margin
        return Slot(m, m, self.width - 2 * m, self.height - 2 * m - self.brand)

    def brand_slot(self) -> Slot:
        """The banner strip, between the top margin and the content area."""
        return Slot(self.margin, self.height - self.margin - self.brand,
                    self.width - 2 * self.margin, self.brand)

    def columns(self, slot: Slot, count: int, *, gutter: float = 10.0) -> list[Slot]:
        """Split a band into narrow full-height columns.

        Rotated and stacked runs are narrow and tall - the opposite shape to a text row -
        so they get columns of their own rather than being squeezed into grid cells.
        """
        width = (slot.width - gutter * (count - 1)) / count
        return [Slot(slot.x + i * (width + gutter), slot.y, width, slot.height)
                for i in range(count)]

    def grid(self, rows: int, cols: int = 1, *, gutter: float = 8.0,
             top: float = 0.0, bottom: float = 0.0) -> Iterator[Slot]:
        """Row-major slots across the content area.

        `top` reserves space at the top of the page - a header zone, whose strings also
        appear in metadata.
        """
        area = self.content
        usable_h = area.height - top - bottom
        cell_w = (area.width - gutter * (cols - 1)) / cols
        cell_h = (usable_h - gutter * (rows - 1)) / rows
        if cell_h <= 0 or cell_w <= 0:
            raise ValueError(f"{rows}x{cols} does not fit the content area")
        for r in range(rows):
            # Top-down reading order, which is what a probe index should follow.
            y = area.y + bottom + usable_h - (r + 1) * cell_h - r * gutter
            for c in range(cols):
                yield Slot(area.x + c * (cell_w + gutter), y, cell_w, cell_h)

    def header_slot(self, height: float = 28.0) -> Slot:
        area = self.content
        return Slot(area.x, area.y + area.height - height, area.width, height)


class OutOfSpace(RuntimeError):
    """The page is full. Raised rather than overlapping or silently dropping a probe."""


@dataclass
class RowPacker:
    """Places variable-width items left to right, wrapping when a row fills.

    A fixed grid has to size every cell for the longest value on the page, so a postcode
    occupies as much room as an email address and most of the sheet is white. Packing to
    measured width is what gets a page from 30 probes to 60 - and probes per page is the
    whole answer to being allowed only one page per tool.
    """

    area: Slot
    row_height: float
    gutter: float = 9.0
    row_gap: float = 2.5

    def __post_init__(self) -> None:
        self._x = self.area.x
        self._top = self.area.y + self.area.height
        self._row_height = 0.0
        self._rows = 1

    @property
    def rows_used(self) -> int:
        return self._rows

    def _wrap(self) -> None:
        self._top -= (self._row_height or self.row_height) + self.row_gap
        self._row_height = 0.0
        self._x = self.area.x
        self._rows += 1

    def place(self, width: float, height: float | None = None) -> Slot:
        """Reserve space on the current row, wrapping first if it will not fit.

        Items hang from the row's top edge and the row advances by its tallest member, so
        one oversized item - a skewed scan, whose bounding box is far taller than the run
        inside it - pushes the next row down instead of colliding with it.
        """
        width = min(width, self.area.width)
        height = height or self.row_height
        if self._x > self.area.x and self._x + width > self.area.x + self.area.width:
            self._wrap()
        self._row_height = max(self._row_height, height)
        if self._top - self._row_height < self.area.y:
            raise OutOfSpace(
                f"no room for an item {width:.0f}x{height:.0f}pt after {self._rows} rows"
            )
        slot = Slot(self._x, self._top - height, width, height)
        self._x += width + self.gutter
        return slot

    def newline(self) -> None:
        """Start a fresh row, e.g. to keep a group of probes together."""
        if self._x > self.area.x:
            self._wrap()

    def rest(self) -> Slot:
        """Whatever vertical space remains, as one slot."""
        self.newline()
        return Slot(self.area.x, self.area.y, self.area.width,
                    max(0.0, self._top - self.area.y))


@dataclass
class Stack:
    """Hands out full-width bands from the top of an area downwards.

    Sections are stacked, not floated: a reader should be able to scan a column without
    the eye jumping, which a greedy left-to-right packer cannot promise.
    """

    area: Slot

    def __post_init__(self) -> None:
        self._y = self.area.y + self.area.height

    @property
    def remaining(self) -> float:
        return self._y - self.area.y

    @property
    def cursor(self) -> float:
        """The y of the next band's top edge."""
        return self._y

    def take(self, height: float) -> Slot:
        if height > self.remaining + 0.01:
            raise OutOfSpace(
                f"needed {height:.0f}pt, {self.remaining:.0f}pt left on the page"
            )
        self._y -= height
        return Slot(self.area.x, self._y, self.area.width, height)

    def skip(self, gap: float) -> None:
        self._y -= min(gap, max(0.0, self.remaining))

    def rest(self) -> Slot:
        return Slot(self.area.x, self.area.y, self.area.width, max(0.0, self.remaining))


@dataclass
class Grid:
    """Aligned cells within a band: every column starts at the same x, rows are uniform.

    The alignment is the point. Ragged starts make a page of short fields look like
    noise, and a reader comparing two probes of the same kind should find them in the
    same column.
    """

    area: Slot
    columns: int
    row_height: float
    gutter: float = 12.0
    row_gap: float = 2.0

    def __post_init__(self) -> None:
        if self.columns < 1:
            raise ValueError("a grid needs at least one column")
        self._index = 0

    @property
    def column_width(self) -> float:
        return (self.area.width - self.gutter * (self.columns - 1)) / self.columns

    @classmethod
    def height_for(cls, count: int, columns: int, row_height: float,
                   row_gap: float = 2.0) -> float:
        rows = max(1, -(-count // max(1, columns)))
        return rows * row_height + (rows - 1) * row_gap

    def cell(self) -> Slot:
        row, column = divmod(self._index, self.columns)
        self._index += 1
        width = self.column_width
        y = self.area.y + self.area.height - (row + 1) * self.row_height - row * self.row_gap
        if y < self.area.y - 0.01:
            raise OutOfSpace(f"grid is full after {row} rows")
        return Slot(self.area.x + column * (width + self.gutter), y, width,
                    self.row_height)

    def skip_to_row(self) -> None:
        """Advance to the start of the next row."""
        while self._index % self.columns:
            self._index += 1
