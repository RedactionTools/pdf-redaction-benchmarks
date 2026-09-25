"""Case families. Each answers a different question; none is a substitute for another.

* `pii-packed`          - many independently scored PII probes on one page
* `extraction-conditions` - one value under varied rendering, scored per subject
* `structural-traps`    - values reachable only through an awkward PDF structure
* `redaction-layers`    - values planted per leak surface
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..types import Case, Conditions, Difficulty
from .builder import (
    CAPTION_CLEARANCE,
    RASTER_CONDITIONS,
    SCALE_SIZE,
    STACK_LEADING,
    CaseBuilder,
)
from .layout import Grid, OutOfSpace, PageLayout, Slot, Stack
from .values import (
    Collision,
    PoolExhausted,
    ValueFactory,
)

# --- extraction-conditions: one factor at a time ---------------------------------------
# The full cross product is 5*6*6*3 = 540 cells against a one-page budget, so vary one
# axis from the control, then add the interactions where tools actually break.

OFAT_CELLS: tuple[Conditions, ...] = (
    Conditions(),  # control
    Conditions(orientation="rot90"),
    Conditions(orientation="rot270"),
    Conditions(orientation="rot180"),
    Conditions(orientation="skew"),
    Conditions(orientation="vertical"),
    Conditions(polarity="inverse"),
    Conditions(polarity="low-contrast"),
    Conditions(polarity="screened"),
    Conditions(polarity="highlighted"),
    Conditions(polarity="on-image"),
    Conditions(provenance="print-clean"),
    Conditions(provenance="print-degraded"),
    Conditions(provenance="hand-block"),
    Conditions(provenance="hand-cursive"),
    Conditions(provenance="hand-mixed"),
    Conditions(scale="pt6"),
    Conditions(scale="pt4"),
)

INTERACTION_CELLS: tuple[Conditions, ...] = (
    Conditions(orientation="rot90", polarity="inverse"),
    Conditions(polarity="inverse", provenance="hand-block"),
    Conditions(provenance="print-degraded", scale="pt6"),
    Conditions(orientation="vertical", polarity="on-image"),
    Conditions(polarity="low-contrast", provenance="hand-mixed"),
    Conditions(orientation="rot180", polarity="low-contrast"),
    Conditions(orientation="skew", provenance="print-degraded"),
    Conditions(polarity="screened", scale="pt6"),
    Conditions(polarity="highlighted", provenance="hand-cursive"),
    Conditions(polarity="on-image", scale="pt6"),
    Conditions(orientation="rot270", provenance="print-clean"),
    # Each of the following is a document someone actually has to redact.
    Conditions(orientation="skew", provenance="hand-cursive"),      # photo of a note
    Conditions(polarity="inverse", provenance="print-degraded"),    # scanned dark header
    Conditions(orientation="rot180", provenance="print-degraded"),  # scanned upside down
    Conditions(polarity="on-image", provenance="print-clean"),      # caption over a photo
    Conditions(polarity="screened", provenance="print-degraded"),   # watermarked scan
    Conditions(polarity="highlighted", scale="pt6"),                # highlighted small print
    Conditions(polarity="low-contrast", scale="pt4"),               # faint fine print
    Conditions(orientation="rot90", provenance="hand-block"),       # turned margin note
    Conditions(polarity="inverse", scale="pt4"),                    # tiny white-on-dark
    Conditions(orientation="rot270", polarity="highlighted"),       # turned, marked up
    Conditions(orientation="skew", scale="pt6"),                    # crooked fine print
    Conditions(orientation="rot90", provenance="print-degraded"),   # turned scan
    Conditions(orientation="rot270", provenance="hand-block"),      # turned block caps
    Conditions(polarity="on-image", provenance="hand-cursive"),     # writing over a photo
    Conditions(polarity="screened", provenance="hand-block"),       # watermarked form
    Conditions(polarity="low-contrast", provenance="print-degraded"),  # faint scan
    Conditions(polarity="highlighted", provenance="print-clean"),   # marked-up scan
    Conditions(polarity="inverse", provenance="hand-cursive"),      # chalk-style writing
    Conditions(orientation="rot180", provenance="hand-mixed"),      # form scanned inverted
    Conditions(orientation="vertical", scale="pt6"),                # stacked fine print
    Conditions(orientation="rot90", scale="pt6"),                   # turned fine print
    Conditions(polarity="screened", provenance="print-clean"),      # watermark over scan
    # Three factors at once: failures compound, and a tool that survives every pair can
    # still fall over here.
    Conditions(orientation="rot90", polarity="inverse", scale="pt6"),
    Conditions(orientation="skew", provenance="print-degraded", scale="pt6"),
    Conditions(orientation="vertical", polarity="on-image", scale="pt6"),
)

CONDITION_CELLS = OFAT_CELLS + INTERACTION_CELLS

#: Orientations whose runs extend vertically far beyond a text row.
TALL_ORIENTATIONS = frozenset({"rot90", "rot270", "vertical"})
#: The longest first-and-last name the condition matrix will draw. Bounded so the tall
#: band - whose depth is this many glyphs - stays within what one page can spare.
MATRIX_NAME_GLYPHS = 14
RASTER_PROVENANCES = RASTER_CONDITIONS["provenance"]


def _skew_row_height(glyphs: int) -> float:
    """Deep enough for a skewed run of `glyphs` glyphs to sit under its caption.

    A 15-degree turn lifts the far end of the run by `width x sin 15`, so the row grows
    with the name the seed chose. A fixed allowance fit short tokens and let a long name
    climb into its own caption - whose ink the scorer then read as the name showing.
    Sized for the widest rendering (the rasterised ones carry padding and wider faces),
    and checked by `check_spatial_isolation`, which refuses a case that does not fit.
    """
    size = SCALE_SIZE["pt10"]
    width = glyphs * size * 0.6  # Courier's advance; the raster faces run narrower
    return CAPTION_CLEARANCE + width * math.sin(math.radians(15.0)) + size * 1.8


def _tall_band_height(glyphs: int) -> float:
    """Deep enough for one stacked run of `glyphs` glyphs, caption included.

    Derived from the value actually drawn rather than the pool maxima, so the band
    follows the name the seed chose instead of assuming the worst any pool could
    produce - a failure that cannot silently appear because a pool grew.
    """
    return CAPTION_CLEARANCE + glyphs * SCALE_SIZE["pt10"] * STACK_LEADING + 14.0


@dataclass(frozen=True, slots=True)
class Skipped:
    """A cell the generator could not draw, and why. Reported, never silently dropped."""

    conditions: Conditions
    reason: str


@dataclass(frozen=True, slots=True)
class Generated:
    case: Case
    skipped: tuple[Skipped, ...] = ()


#: What each page is for, printed on the page itself. A case is opened by an operator,
#: a vendor and sometimes a disputing third party, none of whom have the docs to hand;
#: a sheet of invented identifiers with no explanation invites exactly the wrong guess
#: about what it is.
PURPOSES: dict[str, str] = {
    "pii-packed": (
        "Synthetic benchmark page - every value below is invented and refers to no real "
        "person. Purpose: measure whether a redaction tool removes the personal data "
        "while leaving the document references intact. Both failures are scored."
    ),
    "extraction-conditions": (
        "Synthetic benchmark page - every value below is invented and refers to no real "
        "person. Purpose: measure which rendering conditions a tool can read at all. One "
        "subject - a single first-and-last name - appears under every orientation, "
        "polarity, provenance and size, so a cell measures its condition and nothing "
        "else; a copy surviving anywhere means the subject is still disclosed."
    ),
    "structural-traps": (
        "Synthetic benchmark page - every value below is invented and refers to no real "
        "person. Purpose: measure whether a tool reaches values that are in the file but "
        "not plainly on the page - invisible text, annotations, hidden layers and an "
        "earlier revision. Each trap is repeated so one result is not a fluke."
    ),
    "redaction-layers": (
        "Synthetic benchmark page - every value below is invented and refers to no real "
        "person. Purpose: measure which leak surfaces a tool's redaction actually "
        "reaches. One value is planted per surface, so a survivor names the surface that "
        "failed."
    ),
}

ROW_HEIGHT = 20.0
SECTION_GAP = 5.0
TITLE_HEIGHT = 9.0

#: (title, columns, field kinds). Columns are chosen to fit the widest field in the
#: group at 10pt - email needs half the page, a plate needs a quarter - so every value
#: sits inside its cell and the columns line up down the sheet. `None` is a distractor,
#: mixed into each section rather than pooled at the foot of the page, where their
#: position would be a tell.
PII_SECTIONS: tuple[tuple[str, int, tuple[str | None, ...]], ...] = (
    ("PARTY", 2, ("person", "email", "address", None, "person", "email",
                  "address", "person", "email", None)),
    ("CONTACT", 4, ("phone", "postcode", "plate", None, "phone", "postcode",
                    "plate", "phone", "postcode", None, "plate", "phone")),
    ("IDENTIFIERS", 4, ("national_id", "passport", "account", "dob", "national_id",
                        "passport", "account", "dob", None, "national_id", "passport",
                        "account", "dob", None, "national_id", "passport", "account",
                        "national_id", "passport", "account")),
    ("BANKING", 3, ("card", "iban", None, "card", "iban", "card", "iban", None,
                    "card")),
    ("CLINICAL", 4, ("condition", "condition", "dob", None, "condition",
                     "condition", "account", "condition", "condition", None,
                     "condition", "condition")),
    ("DOCUMENT REFERENCES", 4, (None,) * 20),
)


def _intro(b: CaseBuilder, f: ValueFactory, layout: PageLayout, heading: str,
           family: str) -> float:
    """Heading plus the page's purpose; returns the height consumed."""
    purpose = PURPOSES.get(family, "")
    head = layout.header_slot(height=13.0)
    b.boilerplate(heading, head, size=8.0)
    used = 13.0
    if purpose:
        f.reserve_text(purpose)
        area = layout.content
        slot = Slot(area.x, area.y, area.width, area.height - used)
        used += b.note(slot, purpose) + 4.0
    return used


def _reserve_titles(f: ValueFactory, *texts: str) -> None:
    """Register the page's headings before any value is drawn.

    Order is the whole point. A section title registers itself when it is drawn, but by
    then the values above it are already committed, and the factory can only redraw a
    value it has not handed out yet: a name chosen for the first section cannot be
    un-chosen when the fourth section's title turns out to share a run with it.
    """
    for text in texts:
        if text:
            f.reserve_text(text)


def _section(
    b: CaseBuilder, stack: Stack, title: str, columns: int, count: int,
    row_height: float = ROW_HEIGHT,
) -> Grid | None:
    """Reserve a titled band sized for `count` cells, or None if the page is full."""
    body_height = Grid.height_for(count, columns, row_height)
    needed = SECTION_GAP + TITLE_HEIGHT + body_height
    if needed > stack.remaining:
        return None
    stack.skip(SECTION_GAP)
    b.section_title(stack.take(TITLE_HEIGHT), title)
    return Grid(stack.take(body_height), columns, row_height)


def pii_packed(
    case_id: str,
    seed: int,
    *,
    sections: tuple[tuple[str, int, tuple[str | None, ...]], ...] = PII_SECTIONS,
    dataset_revision: str | None = None,
    generated_at: str | None = None,
) -> tuple[CaseBuilder, tuple[Skipped, ...]]:
    """A structured remittance page: titled sections of aligned, equal-width columns."""
    layout = PageLayout()
    b = CaseBuilder(case_id, seed, "pii-packed", layout, dataset_revision, generated_at)
    f = ValueFactory(seed)
    b.reserve_with(f)
    for text in b.brand_strings:
        f.reserve_text(text)
    _reserve_titles(f, "SUPPLIER", *(title for title, _, _ in sections))

    used = _intro(b, f, layout, "REMITTANCE ADVICE - retain for your records",
                  "pii-packed")

    stack = Stack(Slot(layout.content.x, layout.content.y, layout.content.width,
                       layout.content.height - used))
    made = 0
    for title, columns, kinds in sections:
        grid = _section(b, stack, title, columns, len(kinds))
        if grid is None:
            break
        for kind in kinds:
            try:
                value = f.target(kind) if kind else f.unique(f.distractor)
            except (Collision, PoolExhausted):
                continue
            try:
                cell = grid.cell()
            except OutOfSpace:
                break
            difficulty = (Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD)[made % 3]
            b.add_text_probe(value, cell, difficulty=difficulty)
            made += 1

    # Ambiguous: scored and reported apart from the headline.
    grid = _section(b, stack, "SUPPLIER", 2, 2)
    if grid is not None:
        for maker in (f.employer_distractor, f.distractor):
            try:
                b.add_text_probe(f.unique(maker), grid.cell())
            except (Collision, PoolExhausted, OutOfSpace):
                continue

    # Reachable only through metadata: if it survives, the channel was never opened.
    b.add_metadata_probe(f.target("person"), key="Author")
    return b, ()


def _condition_group(cond: Conditions) -> str:
    """Which section a cell belongs to.

    Grouped by how the run sits on the page rather than by which axis varies, because
    that is what governs its shape - and a reader comparing two upright runs should not
    have to find them among the quarter-turned ones.
    """
    if cond.orientation in TALL_ORIENTATIONS:
        return "TURNED AND STACKED"
    # Orientation before provenance: rows are uniform, and one skewed member would
    # otherwise inflate every row of the rasterised section to fit its bounding box.
    if cond.orientation != "horizontal":
        return "ROTATED"
    if cond.provenance in RASTER_PROVENANCES:
        return "RASTERISED - NO TEXT LAYER"
    if cond.scale != "pt10":
        return "SMALL TYPE"
    return "UPRIGHT - POLARITY AND GROUND"


#: Columns per section. Matrix values are single tokens, so the cells are narrow and
#: the page takes four or five across - which is what lets the matrix carry 50+ cells.
CONDITION_SECTIONS: tuple[tuple[str, int], ...] = (
    ("UPRIGHT - POLARITY AND GROUND", 4),
    ("SMALL TYPE", 5),
    ("RASTERISED - NO TEXT LAYER", 4),
    ("ROTATED", 5),
)


def extraction_conditions(
    case_id: str,
    seed: int,
    *,
    cells: Iterable[Conditions] = CONDITION_CELLS,
    dataset_revision: str | None = None,
    generated_at: str | None = None,
) -> tuple[CaseBuilder, tuple[Skipped, ...]]:
    """One probe per condition cell, grouped so like can be compared with like."""
    cells = tuple(cells)
    layout = PageLayout()
    b = CaseBuilder(case_id, seed, "extraction-conditions", layout, dataset_revision,
                    generated_at)
    f = ValueFactory(seed)
    b.reserve_with(f)
    for text in b.brand_strings:
        f.reserve_text(text)
    _reserve_titles(f, "TURNED AND STACKED", *(title for title, _ in CONDITION_SECTIONS))
    used = _intro(b, f, layout, "RENDERING CONDITION MATRIX", "extraction-conditions")

    # One subject for the whole matrix, drawn once and planted in every cell. The
    # variable under test is how the run is rendered, never which value it drew - and
    # the coupling this creates in the scorer is the point, not a defect: the text
    # layers hunt the name across the whole page, so a survivor in any cell reads as a
    # leak in all of them. One survivor is enough to identify the subject, and that is
    # the honest per-subject number for a family that measures extraction.
    subject = f.unique(lambda: f.matrix_person(MATRIX_NAME_GLYPHS))

    skipped: list[Skipped] = []
    renderable: list[Conditions] = []
    for cond in cells:
        reason = CaseBuilder.check_renderable(cond)
        if reason:
            skipped.append(Skipped(cond, reason))
        else:
            renderable.append(cond)

    groups: dict[str, list[Conditions]] = {}
    for cond in renderable:
        groups.setdefault(_condition_group(cond), []).append(cond)

    # The band of turned and stacked runs is reserved *first*, from the foot of the
    # content area. Laying it out last meant it took whatever was left, and a long
    # stacked name then ran off the bottom of the page.
    tall = groups.get("TURNED AND STACKED", [])
    band_height = _tall_band_height(len(subject.text)) if tall else 0.0
    area = Slot(layout.content.x, layout.content.y, layout.content.width,
                layout.content.height - used)
    band_total = (TITLE_HEIGHT + band_height + SECTION_GAP) if tall else 0.0
    stack = Stack(Slot(area.x, area.y + band_total, area.width,
                       area.height - band_total))

    def place(cond: Conditions, slot: Slot) -> None:
        b.add_text_probe(subject, slot, conditions=cond,
                         difficulty=Difficulty.EASY if cond.is_control else Difficulty.HARD,
                         label=False)

    for title, columns in CONDITION_SECTIONS:
        members = groups.get(title, [])
        if not members:
            continue
        if title == "ROTATED":
            # A form cell prints `Name:` before its value, which makes it the widest in
            # the section; last in the grid, it has the empty column beside it to use.
            members = sorted(members, key=lambda c: c.provenance == "hand-mixed")
        # A skewed run's bounding box is far taller than the run inside it.
        row = ROW_HEIGHT + 6.0
        if any(c.provenance in RASTER_PROVENANCES for c in members):
            row += 5.0  # a rasterised run carries its own padding above the baseline
        if any(c.orientation == "skew" for c in members):
            row = max(row, _skew_row_height(len(subject.text)))
        body_height = Grid.height_for(len(members), columns, row)
        if SECTION_GAP + TITLE_HEIGHT + body_height > stack.remaining:
            for cond in members:
                skipped.append(Skipped(cond, "no room left on the page"))
            continue
        stack.skip(SECTION_GAP)
        b.section_title(stack.take(TITLE_HEIGHT), title)
        grid = Grid(stack.take(body_height), columns, row)
        for cond in members:
            place(cond, grid.cell())

    # Quarter-turned and stacked runs are narrow and tall - the opposite shape to a text
    # row - so they get a band of their own columns under everything else.
    if tall:
        # Directly under the last section rather than pinned to the floor - the space was
        # reserved, so it is guaranteed to fit wherever the sections happen to end.
        title_y = stack.cursor - SECTION_GAP - TITLE_HEIGHT
        b.section_title(Slot(area.x, title_y, area.width, TITLE_HEIGHT),
                        "TURNED AND STACKED")
        band = Slot(area.x, title_y - band_height, area.width, band_height)
        for cond, column in zip(tall, layout.columns(band, len(tall)), strict=False):
            place(cond, column)

    return b, tuple(skipped)


def _trap_family(
    case_id: str,
    seed: int,
    family: str,
    heading: str,
    groups: tuple[tuple[str, str, str, int], ...],
    metadata: tuple[tuple[str, str, bool], ...],
    *,
    replicates: int,
    dataset_revision: str | None,
    generated_at: str | None,
) -> tuple[CaseBuilder, tuple[Skipped, ...]]:
    """One titled section per surface, its replicates side by side in one row.

    Replicated because a single probe per surface cannot separate a real failure from a
    fluke; sectioned because the replicates are only comparable if they sit together.
    """
    layout = PageLayout()
    b = CaseBuilder(case_id, seed, family, layout, dataset_revision, generated_at)
    f = ValueFactory(seed)
    b.reserve_with(f)
    for text in b.brand_strings:
        f.reserve_text(text)
    _reserve_titles(f, *(title for title, _, _, _ in groups))
    used = _intro(b, f, layout, heading, family)

    stack = Stack(Slot(layout.content.x, layout.content.y, layout.content.width,
                       layout.content.height - used))
    adders = {
        "text": b.add_text_probe,
        "invisible": b.add_invisible_probe,
        "annotation": b.add_annotation_probe,
        "hidden": b.add_hidden_layer_probe,
        "revision": b.add_prior_revision_probe,
    }
    for title, adder_name, kind, columns in groups:
        count = 1 if adder_name == "revision" else replicates
        # Columns follow the widest value the group can produce: an email needs half the
        # page, and a cell too narrow for its value would run into its neighbour.
        # Trap probes draw a box or a caption of their own, so they need a deeper row
        # than a plain label-over-value field.
        grid = _section(b, stack, title, min(columns, count), count,
                        row_height=ROW_HEIGHT + 8.0)
        if grid is None:
            break
        for _ in range(count):
            try:
                value = f.target(kind)
                adders[adder_name](value, grid.cell())
            except (Collision, PoolExhausted, OutOfSpace, ValueError):
                continue

    for i in range(replicates):
        for key, kind, xmp in metadata:
            try:
                b.add_metadata_probe(f.target(kind), key=f"{key}{i or ''}", xmp=xmp)
            except (Collision, PoolExhausted):
                continue
    return b, ()


def structural_traps(
    case_id: str,
    seed: int,
    *,
    replicates: int = 3,
    dataset_revision: str | None = None,
    generated_at: str | None = None,
) -> tuple[CaseBuilder, tuple[Skipped, ...]]:
    """Values reachable only through an awkward structure, not an awkward rendering."""
    return _trap_family(
        case_id, seed, "structural-traps", "STRUCTURAL TRAP PROBES",
        (
            ("VISIBLE CONTROL", "text", "person", 3),
            ("INVISIBLE TEXT - RENDER MODE 3", "invisible", "person", 3),
            ("ANNOTATION CONTENTS", "annotation", "email", 2),
            ("OPTIONAL CONTENT - HIDDEN LAYER", "hidden", "national_id", 3),
            ("EARLIER REVISION", "revision", "card", 1),
        ),
        (("Subject", "passport", False), ("CustomClientRef", "iban", True)),
        replicates=replicates, dataset_revision=dataset_revision,
        generated_at=generated_at,
    )


def redaction_layers(
    case_id: str,
    seed: int,
    *,
    replicates: int = 3,
    dataset_revision: str | None = None,
    generated_at: str | None = None,
) -> tuple[CaseBuilder, tuple[Skipped, ...]]:
    """One value per leak surface the output is checked against, repeated."""
    return _trap_family(
        case_id, seed, "redaction-layers", "LEAK SURFACE PROBES",
        (
            ("RENDERED PIXELS AND CONTENT STREAM", "text", "person", 3),
            ("CONTENT STREAM ONLY", "invisible", "email", 2),
            ("ANNOTATIONS", "annotation", "phone", 3),
            ("OPTIONAL CONTENT", "hidden", "account", 3),
            ("EARLIER REVISION", "revision", "national_id", 1),
        ),
        (("Keywords", "dob", False), ("ClientRecord", "address", True)),
        replicates=replicates, dataset_revision=dataset_revision,
        generated_at=generated_at,
    )


FAMILIES: dict[str, Callable[..., tuple[CaseBuilder, tuple[Skipped, ...]]]] = {
    "pii-packed": pii_packed,
    "extraction-conditions": extraction_conditions,
    "structural-traps": structural_traps,
    "redaction-layers": redaction_layers,
}


def generate(
    family: str,
    out_dir: Path | str,
    *,
    seed: int,
    case_id: str | None = None,
    dataset_revision: str | None = None,
    generated_at: str | None = None,
    **kwargs: Any,
) -> Generated:
    """Build one case of a family and write it to `out_dir/<case_id>/`."""
    if family not in FAMILIES:
        raise KeyError(f"unknown family {family!r}; known: {', '.join(sorted(FAMILIES))}")
    case_id = case_id or f"{family}-{seed:06d}"
    builder, skipped = FAMILIES[family](
        case_id, seed, dataset_revision=dataset_revision,
        generated_at=generated_at, **kwargs
    )
    return Generated(case=builder.emit(out_dir), skipped=skipped)


def generate_split(
    out_dir: Path | str, *, seeds: Iterable[int], families: Iterable[str] = tuple(FAMILIES),
    dataset_revision: str | None = None, generated_at: str | None = None,
) -> list[Generated]:
    """A split is a family list crossed with a seed list - nothing more.

    The public split publishes its seeds with the generator; the leaderboard split keeps
    them private, so it can be regenerated fresh if it looks compromised.
    """
    out = []
    for seed in seeds:
        for family in families:
            out.append(generate(family, out_dir, seed=seed,
                                dataset_revision=dataset_revision,
                                generated_at=generated_at))
    return out
