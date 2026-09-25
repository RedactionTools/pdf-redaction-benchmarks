"""Aligner tests.

The property under test is not "the homography is correct" but the one the scores depend
on: **when the aligner cannot recover the mapping, it says so.** A confident wrong answer
marks the whole page as changed, which reads as a perfect redaction.
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import fakes
import numpy as np

from pdfredeval import engines
from pdfredeval.align import Frame, align, assign_roles, find_marks, homography
from pdfredeval.generate import generate
from pdfredeval.types import BBox, Case, Fiducial

DPI = 150


def _expected_side(case: Case, image: object) -> float:
    return 8.0 * math.hypot(*image.size) / math.hypot(*(case.page_size or (1, 1)))


class AlignBase(unittest.TestCase):
    """One generated case, rendered once: every test here reads the same two images."""

    case: Case
    render: object

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.case = generate("pii-detection", Path(cls._tmp.name), seed=3).case
        cls.render = engines.render(cls.case.pdf_bytes, dpi=DPI)
        cls.frame = Frame.of(cls.case, cls.render, dpi=DPI)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


class FrameTests(AlignBase):
    def test_measured_from_the_render_not_the_arithmetic(self):
        """A rasteriser rounds the page up to whole pixels and fits the page to it."""
        page = self.case.page_size
        self.assertEqual(self.frame.size, self.render.size)
        corner = self.frame.point(0.0, page[1])
        self.assertAlmostEqual(corner[0], 0.0)
        self.assertAlmostEqual(corner[1], 0.0)

    def test_box_maps_top_left_first(self):
        box = BBox(0, 100.0, 700.0, 200.0, 720.0)
        x0, y0, x1, y1 = self.frame.box(box)
        self.assertLess(x0, x1)
        self.assertLess(y0, y1, "pixel y runs downwards, PDF y runs upwards")

    def test_fiducial_marks_land_where_the_page_drew_them(self):
        marks = self.frame.marks(self.case)
        self.assertEqual(set(marks), {"tl", "tr", "bl", "br"})
        self.assertLess(marks["tl"][0], marks["tr"][0])
        self.assertLess(marks["tl"][1], marks["bl"][1])


class DetectionTests(AlignBase):
    def test_all_four_marks_are_found(self):
        marks = find_marks(self.render, expected_side=_expected_side(self.case, self.render))
        self.assertEqual(len(marks), 4)

    def test_exactly_one_is_a_donut(self):
        """Four identical squares would leave a 180-degree flip undetectable."""
        marks = find_marks(self.render, expected_side=_expected_side(self.case, self.render))
        self.assertEqual(sum(1 for m in marks if m.donut), 1)

    def test_roles_land_within_a_pixel_of_the_truth(self):
        truth = self.frame.marks(self.case)
        named = assign_roles(
            find_marks(self.render, expected_side=_expected_side(self.case, self.render)),
            truth,
        )
        self.assertEqual({m.role for m in named}, {"tl", "tr", "bl", "br"})
        for mark in named:
            self.assertLess(math.dist((mark.x, mark.y), truth[mark.role]), 1.5)

    def test_roles_need_a_single_donut(self):
        truth = self.frame.marks(self.case)
        self.assertEqual(assign_roles([], truth), [])


class HomographyTests(unittest.TestCase):
    def test_recovers_a_known_transform(self):
        source = [(0.0, 0.0), (100.0, 0.0), (0.0, 50.0), (100.0, 50.0)]
        destination = [(10.0, 20.0), (210.0, 20.0), (10.0, 120.0), (210.0, 120.0)]
        matrix = homography(list(zip(source, destination, strict=True)))
        for (sx, sy), (dx, dy) in zip(source, destination, strict=True):
            point = matrix @ np.array([sx, sy, 1.0])
            self.assertAlmostEqual(point[0] / point[2], dx, places=6)
            self.assertAlmostEqual(point[1] / point[2], dy, places=6)


class AlignmentTests(AlignBase):
    def test_an_unchanged_page_maps_to_the_identity(self):
        """Not cosmetic: a half-pixel shift resampled draws every glyph into the mask."""
        alignment = align(self.case, self.render, self.frame, source=self.render)
        self.assertEqual(alignment.method, "fiducial")
        self.assertTrue(alignment.confident)
        self.assertTrue(alignment.near_identity())
        self.assertLess(alignment.residual_px, 1.0)

    def test_a_quarter_turned_page_is_recovered(self):
        output = engines.render(fakes.rotate(self.case, 90, dpi=DPI), dpi=DPI)
        alignment = align(self.case, output, self.frame, source=self.render)
        self.assertEqual(alignment.method, "fiducial")
        self.assertTrue(alignment.confident)
        self.assertAlmostEqual(abs(alignment.rotation_deg), 90.0, delta=1.0)
        self.assertFalse(alignment.mirrored)

    def test_a_resized_page_is_recovered(self):
        output = engines.render(fakes.rescale(self.case, 0.75, dpi=DPI), dpi=DPI)
        alignment = align(self.case, output, self.frame, source=self.render)
        self.assertTrue(alignment.confident)
        self.assertAlmostEqual(alignment.rotation_deg, 0.0, delta=1.0)

    def test_a_lost_mark_is_recovered_when_the_rest_confirm_the_page(self):
        output = engines.render(fakes.crop_corner(self.case, dpi=DPI), dpi=DPI)
        alignment = align(self.case, output, self.frame, source=self.render)
        self.assertEqual(alignment.method, "page_box_verified")
        self.assertTrue(alignment.confident)
        self.assertNotIn("tl", alignment.found)
        self.assertTrue(alignment.note)

    def test_a_footer_bar_over_both_bottom_marks_is_recovered(self):
        """SafeRedact's free tier: page rescaled, a bar stamped over the donut."""
        output = engines.render(fakes.footer_stamp(self.case, dpi=DPI, scale=0.8), dpi=DPI)
        alignment = align(self.case, output, self.frame, source=self.render)
        self.assertEqual(alignment.method, "page_box_verified")
        self.assertTrue(alignment.confident)
        self.assertEqual(alignment.found, ("tl", "tr"))
        corner = alignment.apply(float(output.size[0] - 1), float(output.size[1] - 1))
        self.assertAlmostEqual(corner[0], self.render.size[0], delta=3.0)
        self.assertAlmostEqual(corner[1], self.render.size[1], delta=3.0)

    def test_a_half_turn_that_lost_its_donut_is_refused_rather_than_guessed(self):
        """Symmetric corners fit a page turned 180 degrees just as well; only the page
        itself says which way up it is."""
        pdf = fakes.rasterise(self.case, dpi=DPI, rotate=180, white_corner=True)
        alignment = align(self.case, engines.render(pdf, dpi=DPI), self.frame,
                          source=self.render)
        self.assertFalse(alignment.confident)
        self.assertEqual(alignment.method, "page_box")

    def test_a_single_surviving_mark_is_refused(self):
        pdf = fakes.rasterise(self.case, dpi=DPI, white_corner=True, footer=True)
        alignment = align(self.case, engines.render(pdf, dpi=DPI), self.frame,
                          source=self.render)
        self.assertFalse(alignment.confident)
        self.assertEqual(alignment.method, "page_box")

    def test_the_page_box_is_not_confirmed_without_the_input_render(self):
        output = engines.render(fakes.crop_corner(self.case, dpi=DPI), dpi=DPI)
        self.assertFalse(align(self.case, output, self.frame).confident)

    def test_a_case_without_fiducials_is_refused(self):
        blind = Case(
            case_id="x", pdf_path=self.case.pdf_path, probes=self.case.probes,
            page_size=self.case.page_size, fiducials=(),
        )
        alignment = align(blind, self.render, self.frame)
        self.assertEqual(alignment.method, "page_box")
        self.assertFalse(alignment.confident)
        self.assertIn("fiducial", alignment.note)

    def test_the_mapping_round_trips_a_corner(self):
        alignment = align(self.case, self.render, self.frame, source=self.render)
        for point in ((0.0, 0.0), (float(self.render.size[0] - 1), 0.0)):
            mapped = alignment.apply(*point)
            self.assertAlmostEqual(mapped[0], point[0], delta=1.0)
            self.assertAlmostEqual(mapped[1], point[1], delta=1.0)


class FiducialRecordTests(unittest.TestCase):
    def test_round_trips_through_ground_truth(self):
        mark = Fiducial("br", 573.28, 22.0, 8.0, "donut")
        self.assertEqual(Fiducial.from_dict(mark.to_dict()), mark)
