"""Prober tests.

The distinction these defend is `unavailable` versus `clean`. A layer that could not be
read must never report that it found nothing, because "found nothing" is the sentence a
published score turns into "no leak".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fakes

from pdfredeval import engines
from pdfredeval.generate import generate
from pdfredeval.probe import images as images_probe
from pdfredeval.probe import metadata as metadata_probe
from pdfredeval.probe import ocr as ocr_probe
from pdfredeval.probe import probe
from pdfredeval.probe import revisions as revisions_probe
from pdfredeval.probe import text as text_probe
from pdfredeval.probe.base import Layer
from pdfredeval.probe.gates import text_retention
from pdfredeval.thresholds import DEFAULTS
from pdfredeval.types import Case, Conditions

THRESHOLDS = DEFAULTS.but(dpi=120)


class ProbeBase(unittest.TestCase):
    """One `redaction-layers` case: a value planted on every leak surface."""

    case: Case

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.case = generate("redaction-layers", Path(cls._tmp.name), seed=4).case
        cls.pdf = cls.case.pdf_bytes

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def by_trap(self, trap: str):
        return [p for p in self.case.probes if p.trap == trap]


class LayerSeparationTests(ProbeBase):
    def test_hidden_layer_text_is_not_pooled_with_the_page(self):
        """Or the optional-content layer would never catch anything of its own."""
        plain, hidden = text_probe.page_text(text_probe.reader(self.pdf))
        value = self.by_trap("optional_content")[0].value
        self.assertIn(value, hidden)
        self.assertNotIn(value, plain)

    def test_annotation_values_are_their_own_layer(self):
        value = self.by_trap("annotation")[0].value
        found = text_probe.annotation_text(text_probe.reader(self.pdf))
        self.assertIn(value, found)

    def test_invisible_text_is_in_the_content_stream(self):
        """Render mode 3: extractable but unseen - a leak no visual review catches."""
        plain, _ = text_probe.page_text(text_probe.reader(self.pdf))
        self.assertIn(self.by_trap("invisible_text")[0].value, plain)

    def test_an_earlier_generation_is_parsed_separately(self):
        reading = revisions_probe.read(self.pdf)
        self.assertTrue(reading.readable)
        self.assertGreaterEqual(reading.detail["revisions"], 1)
        self.assertIn(self.by_trap("prior_revision")[0].value, reading.text)

    def test_metadata_is_searched_by_value_not_by_key(self):
        """Ground truth records that a probe lives in metadata, never under which key."""
        reading = metadata_probe.read(self.pdf)
        for trap in ("info_dict", "xmp"):
            with self.subTest(trap=trap):
                self.assertIn(self.by_trap(trap)[0].value, reading.text)

    def test_a_file_with_one_generation_reports_no_revisions(self):
        reading = revisions_probe.read(fakes.true_redaction(self.case))
        self.assertTrue(reading.readable)
        self.assertEqual(reading.detail["revisions"], 0)


class UnreadableTests(ProbeBase):
    def test_a_broken_pdf_makes_layers_unavailable_not_clean(self):
        readings = {r.layer: r for r in text_probe.read(fakes.broken_pdf(self.case))}
        for layer in (Layer.CONTENT_STREAM, Layer.ANNOTATION, Layer.OPTIONAL_CONTENT):
            with self.subTest(layer=layer):
                self.assertFalse(readings[layer].readable)
                self.assertTrue(readings[layer].unavailable)

    def test_a_broken_pdf_fails_every_gate(self):
        observations = probe(self.case, fakes.broken_pdf(self.case), thresholds=THRESHOLDS,
                             ocr_enabled=False)
        self.assertFalse(observations.survivability.passed)
        self.assertIn("opens", observations.survivability.failures)

    def test_no_ocr_engine_is_reported_rather_than_skipped(self):
        with mock.patch.object(engines, "ocr_engine", return_value=None):
            reading = ocr_probe.read(object(), self.case)
        self.assertFalse(reading.readable)
        self.assertIn("OCR", reading.unavailable)

    def test_ocr_disabled_is_still_not_clean(self):
        reading = ocr_probe.read(None, self.case, enabled=False)
        self.assertFalse(reading.readable)

    def test_no_renderer_makes_the_pixel_layer_unavailable(self):
        with mock.patch.object(
            engines, "render", side_effect=engines.RendererUnavailable("no rasteriser")
        ):
            observations = probe(self.case, self.pdf, thresholds=THRESHOLDS,
                                 ocr_enabled=False)
        self.assertIsNone(observations.mask)
        self.assertIn("rendered_pixels", observations.unavailable_layers)
        self.assertTrue(observations.readable(Layer.CONTENT_STREAM),
                        "losing the rasteriser must not lose the text layers")


class ChangeMaskTests(ProbeBase):
    def test_an_unchanged_page_leaves_an_empty_mask(self):
        observations = probe(self.case, self.pdf, thresholds=THRESHOLDS, ocr_enabled=False)
        self.assertIsNotNone(observations.mask)
        self.assertEqual(observations.mask.changed_px, 0)

    def test_a_cover_registers_and_stays_local(self):
        target = self.case.targets[0]
        observations = probe(self.case, fakes.black_box(self.case, (target,)),
                             thresholds=THRESHOLDS, ocr_enabled=False)
        self.assertGreater(observations.mask.coverage(target.bbox), 0.98)
        untouched = [p for p in self.case.targets if p.bbox and p.id != target.id]
        self.assertLess(observations.mask.coverage(untouched[0].bbox), 0.5)

    def test_the_page_rewritten_flag_appears_when_everything_moves(self):
        observations = probe(self.case, fakes.rescale(self.case, 0.6, dpi=120),
                             thresholds=THRESHOLDS, ocr_enabled=False)
        detail = observations.readings[Layer.RENDERED_PIXELS].detail
        self.assertIn("page_rewritten", detail)


class OcrPassTests(unittest.TestCase):
    def test_rotations_follow_the_page(self):
        """A stacked or quarter-turned run is invisible to a single upright pass."""
        case = Case(case_id="x", pdf_path=Path("x.pdf"), probes=())
        self.assertEqual(ocr_probe.rotations(case), (0,))

    def test_both_polarities_are_read(self):
        from PIL import Image

        image = Image.new("RGB", (8, 8), (255, 255, 255))
        labels = [label for label, _, _ in ocr_probe.passes(image, (0,))]
        self.assertEqual(labels, ["rot0", "rot0-inverse"])


    def test_a_word_read_off_a_turned_pass_lands_where_it_was_drawn(self):
        """Or a quarter-turned leak would be attributed to whatever sits at its mirror."""
        from PIL import Image, ImageDraw

        page = Image.new("RGB", (40, 20), (255, 255, 255))
        ImageDraw.Draw(page).rectangle((5, 2, 11, 6), fill=(0, 0, 0))
        drawn = (5, 2, 12, 7)
        for angle in (0, 90, 180, 270):
            turned = next(img for _, a, img in ocr_probe.passes(page, (angle,)) if a == angle)
            found = Image.eval(turned.convert("L"), lambda v: 255 - v).getbbox()
            self.assertEqual(ocr_probe.unrotate(found, angle, page.size), drawn, angle)


class LocatedTextTests(unittest.TestCase):
    """A located layer judges each probe by the text drawn at it."""

    def setUp(self) -> None:
        from pdfredeval.types import BBox, Probe, ProbeKind

        self.probe = Probe(id="t001", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                           value="Freya Yamamoto", units=("Freya Yamamoto",),
                           bbox=BBox(page=0, x0=10, y0=10, x1=50, y1=20))

    def haystack(self, spans):
        from pdfredeval.probe.base import Reading
        from pdfredeval.score.rules import _haystack

        return _haystack(self.probe, Reading(Layer.CONTENT_STREAM, "page text", spans=spans))

    def test_text_in_another_probes_box_is_left_out(self):
        from pdfredeval.probe.base import Span

        text, scope = self.haystack((
            Span("Freya Yamamoto", (0, 0, 1, 1), frozenset({"t002"})),
            Span("[t001] ", (0, 0, 1, 1)),
            Span("Em", (0, 0, 1, 1), frozenset({"t001"})),
        ))
        self.assertEqual((text, scope), ("[t001] Em", "probe"))

    def test_unplaced_layers_are_judged_page_wide(self):
        self.assertEqual(self.haystack(None), ("page text", "page"))


class RevealedGlyphTests(unittest.TestCase):
    """The pixel verdict asks whether the ink still shows, not whether pixels changed."""

    GROUND, INK, COVER = (158, 158, 158), (110, 110, 110), (128, 128, 128)

    def page(self, ground, strokes=(), fill=None, cover=None, label=None, bold=0):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (120, 40), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 109, 29), fill=ground)
        for x in strokes:
            draw.rectangle((x - bold, 13, x + 2 + bold, 26), fill=fill or self.INK)
        if cover:
            draw.rectangle((9, 9, 110, 30), fill=cover)
        if label:
            for x in range(14, 104, 12):
                draw.rectangle((x, 14, x + 2, 25), fill=label)
        return image

    def revealed(self, before, after):
        from pdfredeval.align import IDENTITY, Frame
        from pdfredeval.probe.mask import build
        from pdfredeval.types import BBox

        frame = Frame(page_size=(120.0, 40.0), size=(120, 40))
        change = build(before, after, IDENTITY, frame)
        glyphs = change.revealed(BBox(page=0, x0=10, y0=10, x1=110, y1=30),
                                 delta_px=24, window_px=9, merge_px=1, floor_px=0)
        return glyphs.revealed

    def setUp(self) -> None:
        self.strokes = tuple(range(14, 104, 8))
        self.before = self.page(self.GROUND, self.strokes)

    def test_a_cover_the_colour_of_the_ink_hides_it(self):
        """Low-contrast text under a grey cover: the ground changes, the ink does not."""
        after = self.page(self.GROUND, cover=self.COVER)
        self.assertEqual(self.revealed(self.before, after), 0.0)

    def test_text_left_in_place_is_revealed(self):
        self.assertGreater(self.revealed(self.before, self.before), 0.9)

    def test_text_redrawn_bolder_is_still_revealed(self):
        """Every glyph pixel changed, and every glyph is legible."""
        after = self.page(self.GROUND, self.strokes, fill=(20, 20, 20), bold=1)
        # A thicker stroke fills more of the window, so fewer of its pixels stand out
        # from their neighbourhood; well over half still do.
        self.assertGreater(self.revealed(self.before, after), 0.5)

    def test_a_label_drawn_over_the_cover_is_not_the_value(self):
        """Light marks on a dark cover stand out the other way from dark ink."""
        after = self.page(self.GROUND, cover=(0, 0, 0), label=(255, 255, 255))
        self.assertEqual(self.revealed(self.before, after), 0.0)


class TextRetentionTests(unittest.TestCase):
    def test_removing_only_the_targets_costs_nothing(self):
        source = "Invoice 42 for Whitfield Diffie, due Friday"
        output = "Invoice 42 for , due Friday"
        self.assertGreater(text_retention(source, output, ("Whitfield Diffie",)), 0.95)

    def test_flattening_the_page_destroys_retention(self):
        self.assertEqual(text_retention("Invoice 42 for Diffie", "", ("Diffie",)), 0.0)

    def test_nothing_to_retain(self):
        self.assertEqual(text_retention("", "", ()), 1.0)


class ImageHashTests(unittest.TestCase):
    def test_a_rescaled_image_keeps_its_hash(self):
        from PIL import Image, ImageDraw

        picture = Image.new("RGB", (128, 128), (250, 250, 250))
        draw = ImageDraw.Draw(picture)
        draw.ellipse((20, 20, 100, 100), fill=(20, 40, 160))
        smaller = picture.resize((64, 64), Image.Resampling.LANCZOS)
        self.assertLessEqual(
            images_probe.hamming(images_probe.phash(picture),
                                 images_probe.phash(smaller)),
            DEFAULTS.tau_hash,
        )

    def test_a_different_picture_hashes_apart(self):
        from PIL import Image, ImageDraw

        a = Image.new("RGB", (128, 128), (255, 255, 255))
        ImageDraw.Draw(a).rectangle((0, 0, 64, 128), fill=(0, 0, 0))
        b = Image.new("RGB", (128, 128), (255, 255, 255))
        ImageDraw.Draw(b).rectangle((0, 0, 128, 64), fill=(0, 0, 0))
        self.assertGreater(
            images_probe.hamming(images_probe.phash(a), images_probe.phash(b)),
            DEFAULTS.tau_hash,
        )


class ConditionsRotationTests(unittest.TestCase):
    def test_vertical_asks_for_turned_passes(self):
        """`vertical` is not `rot90`, but a turned pass sometimes reads it anyway."""
        probe_with = Conditions(orientation="vertical")
        self.assertIn(90, ocr_probe._ROTATION_FOR[probe_with.orientation])
