"""The aligner: recover the output-to-ground-truth mapping from the fiducials.

The tool hands back a *re-encoded* PDF. It may be recompressed, rasterised, rotated,
resized or cropped, and our ground-truth coordinates do not map onto it. Alignment is
therefore a pipeline stage rather than an assumption, and it is solvable only because the
generator prints four registration marks (docs/core.md).

What makes this worth care: **a wrong alignment publishes a flattering score.** Every
raster check compares the output against the input, so a mapping that is off by a few
degrees marks the whole page as changed - which reads as "everything was redacted" and
gives a tool that did nothing a leak rate of zero. So the aligner reports how confident it
is, and the probers that depend on it decline to produce a verdict when it is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .types import Case, Fiducial

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: A pixel darker than this is ink. Fiducials are painted `0 g`; the margin they sit in
#: is empty, so the threshold only has to survive re-encoding, not separate glyphs.
DARK = 110

#: Corner window side, as a fraction of the page's shorter side. Wide enough to hold a
#: mark that has drifted under rescaling, narrow enough to stay out of the body text.
WINDOW = 0.18

#: A mark's area may be this far off the expected value and still be believed - tools
#: rescale, and a JPEG round trip erodes an edge.
AREA_TOLERANCE = (0.25, 4.0)

#: A page-box mapping - the output page is the input page, rescaled - is trusted only when
#: the page kept its shape (per-axis scales within this fraction of each other), at least
#: this many marks land where it puts them, and the page reads upright under it.
PAGE_BOX_ANISOTROPY = 0.01
PAGE_BOX_MIN_MARKS = 2
#: Upright must correlate with the input at least this well, and beat the 180-degree turn
#: and both mirrorings by this margin. Measured on the four families and real outputs:
#: upright 0.44-0.99, the best wrong orientation at most 0.41, never closer than 0.26;
#: a half-turned page scores 0.08-0.16 upright against 0.98 turned.
UPRIGHT_MIN = 0.30
UPRIGHT_MARGIN = 0.20


@dataclass(frozen=True, slots=True)
class Mark:
    """A registration mark as found in a rendered page, in pixels."""

    x: float
    y: float
    area: int
    width: int
    height: int
    donut: bool
    role: str | None = None


@dataclass(frozen=True, slots=True)
class Alignment:
    """How output pixels map onto input pixels, and how much to trust that.

    `matrix` is 3x3 homogeneous, output raster -> input raster, both at the same DPI.
    """

    matrix: tuple[tuple[float, ...], ...]
    method: str  # "fiducial" | "affine" | "page_box_verified" | "page_box"
    residual_px: float
    rotation_deg: float
    scale: float
    mirrored: bool
    found: tuple[str, ...]
    confident: bool
    note: str = ""

    @property
    def array(self) -> Any:
        return np.array(self.matrix, dtype=float)

    def apply(self, x: float, y: float) -> tuple[float, float]:
        """Map one output pixel into the input frame."""
        m = self.array
        denominator = m[2, 0] * x + m[2, 1] * y + m[2, 2]
        if abs(denominator) < 1e-12:  # pragma: no cover - degenerate by construction
            return (float("nan"), float("nan"))
        return (
            float((m[0, 0] * x + m[0, 1] * y + m[0, 2]) / denominator),
            float((m[1, 0] * x + m[1, 1] * y + m[1, 2]) / denominator),
        )

    def near_identity(self, tolerance: float = 0.002) -> bool:
        """True when the mapping is a no-op to within a fraction of a pixel.

        Worth asking, because resampling is not free: warping a page through an
        almost-identity homography shifts every glyph edge by a fraction of a pixel, and
        the interpolation that follows lights up the change mask along every stroke on
        the page. A tool that preserved the geometry should produce an empty mask, not a
        faint outline of the document.
        """
        m = self.array
        linear_off = float(np.abs(m[:2, :2] - np.eye(2)).max())
        shift = max(abs(float(m[0, 2])), abs(float(m[1, 2])))
        projective = float(np.abs(m[2, :2]).max())
        return linear_off <= tolerance and shift <= 0.5 and projective <= 1e-9

    def warp(self, image: Image, size: tuple[int, int]) -> Image:
        """Warp an output render into the input's frame, at `size`.

        Pillow's perspective transform is specified destination-to-source, so it needs
        the inverse of `matrix`; that is the only reason the inverse is taken here.
        """
        from PIL import Image as PILImage

        if self.near_identity() and image.size == size:
            return image

        inverse = np.linalg.inv(self.array)
        inverse = inverse / inverse[2, 2]
        coefficients = tuple(float(v) for v in inverse.flatten()[:8])
        return image.transform(
            size, PILImage.Transform.PERSPECTIVE, coefficients,
            resample=PILImage.Resampling.BICUBIC, fillcolor=(255, 255, 255),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "residual_px": round(self.residual_px, 3),
            "rotation_deg": round(self.rotation_deg, 2),
            "scale": round(self.scale, 5),
            "mirrored": self.mirrored,
            "found": list(self.found),
            "confident": self.confident,
            "note": self.note,
            "matrix": [[round(v, 8) for v in row] for row in self.matrix],
        }


IDENTITY = Alignment(
    matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    method="identity", residual_px=0.0, rotation_deg=0.0, scale=1.0,
    mirrored=False, found=(), confident=True,
    note="identity mapping assumed",
)


# --- ground-truth side ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    """How ground-truth points land on the pixels of the *input* render.

    Not `x * dpi / 72`. A rasteriser rounds a 595.28pt page up to a whole number of
    pixels and then fits the page to it, so the analytic formula is off by up to a pixel
    at the far edge - and the change mask is compared against that render, not against
    the arithmetic. Measuring the frame from the render we actually have removes a
    systematic error from every coverage number.
    """

    page_size: tuple[float, float]
    size: tuple[int, int]

    @classmethod
    def of(cls, case: Case, image: Image | None = None, dpi: int = 300) -> Frame:
        page = case.page_size or (612.0, 792.0)
        if image is not None:
            return cls(page, image.size)
        scale = dpi / 72.0
        return cls(page, (round(page[0] * scale), round(page[1] * scale)))

    @property
    def dpi(self) -> float:
        """Effective horizontal resolution, for anything measured in points."""
        return 72.0 * self.size[0] / self.page_size[0]

    def point(self, x: float, y: float) -> tuple[float, float]:
        """PDF points (origin bottom-left) to raster pixels (origin top-left)."""
        return (
            x * self.size[0] / self.page_size[0],
            (self.page_size[1] - y) * self.size[1] / self.page_size[1],
        )

    def box(self, bbox: Any) -> tuple[int, int, int, int]:
        x0, y0 = self.point(bbox.x0, bbox.y1)
        x1, y1 = self.point(bbox.x1, bbox.y0)
        return (int(math.floor(x0)), int(math.floor(y0)),
                int(math.ceil(x1)), int(math.ceil(y1)))

    def marks(self, case: Case) -> dict[str, tuple[float, float]]:
        """Fiducial centres in input-raster pixels, keyed by role."""
        return {f.role: self.point(f.x, f.y) for f in case.fiducials}


# --- detection -----------------------------------------------------------------------


def _components(dark: Any, min_area: int) -> list[tuple[list[int], list[int]]]:
    """Connected components over the dark pixels of a window, 8-connected.

    Union-find over the dark coordinates only. Labelling the whole window would mean
    millions of cells in Python; a corner of a benchmark page holds a few thousand dark
    pixels, and the marks are the largest blobs among them.
    """
    ys, xs = np.nonzero(dark)
    if ys.size == 0:
        return []
    index = {(int(y), int(x)): i for i, (y, x) in enumerate(zip(ys, xs, strict=True))}
    parent = list(range(ys.size))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Half the neighbourhood is enough: every pair is visited once from one side.
    for (y, x), i in index.items():
        for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
            j = index.get((y + dy, x + dx))
            if j is not None:
                union(i, j)

    groups: dict[int, tuple[list[int], list[int]]] = {}
    for (y, x), i in index.items():
        bucket = groups.setdefault(find(i), ([], []))
        bucket[0].append(y)
        bucket[1].append(x)
    return [g for g in groups.values() if len(g[0]) >= min_area]


def _classify(
    patch: Any, offset: tuple[int, int], group: tuple[list[int], list[int]]
) -> Mark:
    """Measure one component. `group` is in patch coordinates; `offset` puts it back."""
    ys, xs = np.array(group[0]), np.array(group[1])
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    height, width = y1 - y0 + 1, x1 - x0 + 1

    # The `br` mark is a filled square with a white square painted concentrically on
    # top - a bright centre, not a hole. Sample the middle third and ask if it is light.
    inset_y, inset_x = height // 3, width // 3
    core = patch[y0 + inset_y : y1 - inset_y + 1, x0 + inset_x : x1 - inset_x + 1]
    donut = bool(core.size and float(core.mean()) > 160)

    # +0.5 converts a pixel index into the centre of that pixel, which is the space the
    # ground-truth geometry is expressed in. Without it every mark reads half a pixel up
    # and left of where it is.
    return Mark(
        x=float(xs.mean()) + offset[1] + 0.5,
        y=float(ys.mean()) + offset[0] + 0.5,
        area=int(ys.size),
        width=width,
        height=height,
        donut=donut,
    )


def find_marks(image: Image, *, expected_side: float) -> list[Mark]:
    """Locate registration marks in the four corners of a rendered page.

    `expected_side` is the mark's side in pixels, derived from the page diagonal so it
    survives rotation.
    """
    grey = np.asarray(image.convert("L"))
    height, width = grey.shape
    window = max(24, int(WINDOW * min(height, width)))
    lo, hi = AREA_TOLERANCE
    area = expected_side * expected_side

    marks: list[Mark] = []
    for top in (0, max(0, height - window)):
        for left in (0, max(0, width - window)):
            patch = grey[top : top + window, left : left + window]
            dark = patch < DARK
            best: Mark | None = None
            for group in _components(dark, int(area * lo * 0.5)):
                mark = _classify(patch, (top, left), group)
                # A mark is square and solid. Text strokes are neither, and a rule is
                # long and thin, so the aspect test alone throws most of them out.
                aspect = mark.width / max(1, mark.height)
                fill = mark.area / max(1, mark.width * mark.height)
                if not (0.7 <= aspect <= 1.4) or fill < 0.55:
                    continue
                if not (area * lo <= mark.width * mark.height <= area * hi):
                    continue
                if best is None or abs(mark.area - area) < abs(best.area - area):
                    best = mark
            if best is not None:
                marks.append(best)
    return marks


def assign_roles(marks: Sequence[Mark], truth: dict[str, tuple[float, float]]) -> list[Mark]:
    """Name each found mark, using the donut as the anchor.

    The donut is `br` by construction. The other three are told apart by their distance
    from it: on a page that is not square those three distances are distinct, and
    distance survives rotation *and* mirroring - which a left/right rule does not.
    """
    donuts = [m for m in marks if m.donut]
    if len(donuts) != 1 or len(marks) < 3 or "br" not in truth:
        return []

    anchor = donuts[0]
    others = [m for m in marks if m is not anchor]
    reference = {
        role: math.dist(truth[role], truth["br"])
        for role in ("tl", "tr", "bl")
        if role in truth
    }
    if len(reference) < 3:
        return []

    # Scale-free: compare each observed distance against the reference ones, normalised
    # by the largest in each set, so a rescaled output still matches.
    span = max(reference.values())
    observed = {m: math.dist((m.x, m.y), (anchor.x, anchor.y)) for m in others}
    observed_span = max(observed.values()) or 1.0

    named: list[Mark] = [Mark(anchor.x, anchor.y, anchor.area, anchor.width,
                              anchor.height, True, "br")]
    taken: set[str] = set()
    for mark, distance in sorted(observed.items(), key=lambda kv: -kv[1]):
        ratio = distance / observed_span
        role = min(
            (r for r in reference if r not in taken),
            key=lambda r: abs(reference[r] / span - ratio),
        )
        taken.add(role)
        named.append(Mark(mark.x, mark.y, mark.area, mark.width, mark.height,
                          mark.donut, role))
    return named


# --- fitting -------------------------------------------------------------------------


def homography(pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> Any:
    """Solve for the 3x3 mapping source -> destination from four correspondences."""
    a = np.zeros((8, 8), dtype=float)
    b = np.zeros(8, dtype=float)
    for i, ((sx, sy), (dx, dy)) in enumerate(pairs[:4]):
        a[2 * i] = [sx, sy, 1, 0, 0, 0, -sx * dx, -sy * dx]
        a[2 * i + 1] = [0, 0, 0, sx, sy, 1, -sx * dy, -sy * dy]
        b[2 * i] = dx
        b[2 * i + 1] = dy
    solution = np.linalg.solve(a, b)
    return np.append(solution, 1.0).reshape(3, 3)


def affine(pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> Any:
    """Least-squares affine fit; the fallback when only three marks were found."""
    source = np.array([[sx, sy, 1.0] for (sx, sy), _ in pairs])
    destination = np.array([[dx, dy] for _, (dx, dy) in pairs])
    solution, *_ = np.linalg.lstsq(source, destination, rcond=None)
    return np.vstack([solution.T, [0.0, 0.0, 1.0]])


def _shape_residual(
    pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[float, float]:
    """Best-fit scale between the two point sets, and the RMS error it leaves.

    A homography through exactly four points fits them perfectly, so its own residual
    says nothing. What *can* disagree is the shape: if the marks we found are not the
    marks we think they are, their pairwise distances will not be a uniform scaling of
    the ground truth's. That mismatch is the honest confidence signal.
    """
    source = [p[0] for p in pairs]
    destination = [p[1] for p in pairs]
    observed, reference = [], []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            observed.append(math.dist(source[i], source[j]))
            reference.append(math.dist(destination[i], destination[j]))
    if not reference or not any(reference):
        return (1.0, float("inf"))
    scale = float(np.dot(observed, reference) / np.dot(reference, reference))
    error = np.array(observed) - scale * np.array(reference)
    return (1.0 / scale if scale else 1.0, float(np.sqrt(np.mean(error**2))))


def _decompose(matrix: Any) -> tuple[float, float, bool]:
    """Rotation in degrees, uniform scale, and whether the mapping flips handedness."""
    a, b, d, e = matrix[0, 0], matrix[0, 1], matrix[1, 0], matrix[1, 1]
    rotation = math.degrees(math.atan2(d, a))
    scale = math.sqrt(abs(a * e - b * d))
    return (rotation, scale, bool(a * e - b * d < 0))


# --- the stage -----------------------------------------------------------------------


def _expected_side(case: Case, image: Image) -> float:
    """A mark's side in pixels, from the page diagonal - which rotation preserves."""
    page = case.page_size or (612.0, 792.0)
    size = (case.fiducials[0].size if case.fiducials else 8.0)
    return size * math.hypot(*image.size) / math.hypot(*page)


def _measured(case: Case, source: Image, frame: Frame) -> dict[str, tuple[float, float]]:
    """Where the marks actually land in the input's own render."""
    found = assign_roles(
        find_marks(source, expected_side=_expected_side(case, source)),
        frame.marks(case),
    )
    return {m.role: (m.x, m.y) for m in found if m.role}


def _confirm_page_box(
    raw: Sequence[Mark],
    truth: dict[str, tuple[float, float]],
    matrix: Any,
    output: Image,
    source: Image | None,
    max_residual_px: float,
) -> tuple[dict[str, float], float] | None:
    """(distance per confirming mark, RMS) when a page-box mapping can be trusted.

    For a tool that kept the page and only rescaled it, but stamped over a corner - a
    free tier's footer bar is the usual culprit. Losing the donut costs `assign_roles`
    its anchor, but a page-box mapping needs no anchor: it predicts where every mark
    sits, and marks found there confirm it.

    Mark positions alone cannot rule out a half turn or a mirroring - the four corners
    are symmetric, and only the donut tells them apart. So the page must also read
    upright: it has to match the input far better as it stands than turned or flipped.
    """
    if source is None:
        return None
    sx, sy = float(matrix[0, 0]), float(matrix[1, 1])
    if abs(sx - sy) > PAGE_BOX_ANISOTROPY * max(sx, sy):
        return None

    hits: dict[str, float] = {}
    for mark in raw:
        at = (mark.x * sx, mark.y * sy)
        role, distance = min(
            ((role, math.dist(at, point)) for role, point in truth.items()),
            key=lambda pair: pair[1],
        )
        if distance <= max_residual_px and distance < hits.get(role, math.inf):
            hits[role] = distance
    if len(hits) < PAGE_BOX_MIN_MARKS:
        return None

    upright, turned = _orientation_scores(output, source)
    if upright < UPRIGHT_MIN or upright - max(turned) < UPRIGHT_MARGIN:
        return None
    residual = math.sqrt(sum(d * d for d in hits.values()) / len(hits))
    return hits, residual


def _orientation_scores(output: Image, source: Image) -> tuple[float, list[float]]:
    """Correlation with the input: as it stands, then turned 180 and mirrored both ways.

    On high-passed thumbnails: the lines of text a page is made of carry the comparison,
    and a stamp - a solid footer bar, a band of black boxes - adds only its outline. A
    plain correlation lets one dark bar outweigh the whole page. Most of a page is
    untouched by any tool, and it is only in the right place one way up.
    """
    from PIL import Image as PILImage
    from PIL import ImageFilter, ImageOps

    size = (256, max(1, round(256 * source.size[1] / source.size[0])))

    def thumb(image: Image) -> Any:
        small = image.convert("L").resize(size, PILImage.Resampling.BILINEAR)
        detail = (np.asarray(small, dtype=float)
                  - np.asarray(small.filter(ImageFilter.GaussianBlur(3)), dtype=float))
        detail = detail - detail.mean()
        return detail / (np.sqrt((detail * detail).sum()) or 1.0)

    reference = thumb(source)
    grey = output.convert("L")
    scores = [float((reference * thumb(candidate)).sum()) for candidate in (
        grey, grey.rotate(180), ImageOps.mirror(grey), ImageOps.flip(grey),
    )]
    return scores[0], scores[1:]


def align(
    case: Case,
    output: Image,
    frame: Frame,
    *,
    source: Image | None = None,
    max_residual_px: float = 6.0,
) -> Alignment:
    """Map the rendered output onto the input's raster frame.

    Falls back down a ladder - four marks, three marks, a page box the surviving marks
    confirm, a bare page box - and says which rung it landed on. Only the top three are
    trusted by the raster checks.

    When the input's own render is passed as `source`, the marks *found in it* are used
    as the destination rather than the arithmetic positions. Both sides are then measured
    the same way, so a tool that changed nothing maps to exactly the identity instead of
    to a half-pixel shift - and a half-pixel shift, resampled, draws a faint outline of
    every glyph on the page into the change mask.
    """
    truth = frame.marks(case)
    if truth and source is not None:
        truth = {**truth, **_measured(case, source, frame)}
    if not truth or not case.page_size:
        return Alignment(
            IDENTITY.matrix, "page_box", float("inf"), 0.0, 1.0, False, (), False,
            "ground truth records no fiducials, so nothing anchors the output",
        )

    raw = find_marks(output, expected_side=_expected_side(case, output))
    marks = assign_roles(raw, truth)
    pairs = [((m.x, m.y), truth[m.role]) for m in marks if m.role in truth]
    found = tuple(sorted(m.role for m in marks if m.role))

    fitted: Alignment | None = None
    if len(pairs) >= 3:
        scale, residual = _shape_residual(pairs)
        matrix = homography(pairs) if len(pairs) >= 4 else affine(pairs)
        rotation, fitted_scale, mirrored = _decompose(matrix)
        confident = residual <= max_residual_px
        fitted = Alignment(
            matrix=tuple(tuple(float(v) for v in row) for row in matrix),
            method="fiducial" if len(pairs) >= 4 else "affine",
            residual_px=residual,
            rotation_deg=rotation,
            scale=fitted_scale,
            mirrored=mirrored,
            found=found,
            confident=confident,
            note="" if confident else (
                f"the four marks do not sit in the shape the ground truth describes "
                f"(RMS {residual:.1f}px); raster checks are not scored from this"
            ),
        )
        if confident:
            return fitted

    # Nothing anchored: assume the page is the page. Enough to render a side-by-side for
    # a human; enough to score from only if the marks that are left and the page itself
    # both confirm it.
    matrix = np.array([
        [frame.size[0] / max(1, output.size[0]), 0.0, 0.0],
        [0.0, frame.size[1] / max(1, output.size[1]), 0.0],
        [0.0, 0.0, 1.0],
    ])
    # Also where too few marks were named to fit - a mark lost to a stamp costs the
    # scale-free role assignment its reference shape, and the fit comes out skewed.
    confirmed = _confirm_page_box(raw, truth, matrix, output, source, max_residual_px)
    if confirmed is None and fitted is not None:
        return fitted
    if confirmed is not None:
        hits, residual = confirmed
        return Alignment(
            matrix=tuple(tuple(float(v) for v in row) for row in matrix),
            method="page_box_verified",
            residual_px=residual,
            rotation_deg=0.0,
            scale=float(matrix[0, 0]),
            mirrored=False,
            found=tuple(sorted(hits)),
            confident=True,
            note=(
                f"{4 - len(hits)} registration mark(s) hidden, most likely by the tool's "
                f"own stamp; the page-box mapping is confirmed by {', '.join(sorted(hits))} "
                f"landing within {residual:.1f}px and by the page reading upright"
            ),
        )
    return Alignment(
        matrix=tuple(tuple(float(v) for v in row) for row in matrix),
        method="page_box",
        residual_px=float("inf"),
        rotation_deg=0.0,
        scale=float(matrix[0, 0]),
        mirrored=False,
        found=found,
        confident=False,
        note=(
            f"found {len(found)} of 4 registration marks ({', '.join(found) or 'none'}); "
            f"fell back to the page box, which cannot be trusted for coverage"
        ),
    )


__all__ = [
    "Alignment", "Fiducial", "Frame", "IDENTITY", "Mark", "affine", "align",
    "assign_roles", "find_marks", "homography",
]
