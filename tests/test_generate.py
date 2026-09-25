"""Generator tests. The invariants here are the ones a bad case would corrupt silently."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pdfredeval.generate import (
    CONDITION_CELLS,
    FAMILIES,
    INTERACTION_CELLS,
    OFAT_CELLS,
    CaseBuilder,
    PageLayout,
    ValueFactory,
    check_xref,
    generate,
)
from pdfredeval.generate.branding import brand_strings
from pdfredeval.generate.builder import RASTER_CONDITIONS, SCALE_SIZE
from pdfredeval.generate.families import MATRIX_NAME_GLYPHS
from pdfredeval.generate.layout import Grid
from pdfredeval.generate.pdf import COURIER
from pdfredeval.generate.textraster import font_digest
from pdfredeval.generate.values import (
    SPEC,
    PoolExhausted,
    Value,
    iban_check_digits,
    luhn_check_digit,
)
from pdfredeval.types import Case, Channel, Conditions, ProbeKind

#: Pinned so byte-comparisons are about the generator, not the clock.
STAMP = "2026-01-01T00:00:00Z"
VERSION = importlib.metadata.version("pdfredeval")


def content_streams(pdf: bytes) -> bytes:
    """Every page content stream in the file, across revisions.

    Identified by the fiducial operators, which only the content stream carries - the
    metadata stream is also a `stream` object, and searching from `/Type /Page` finds it
    instead, since objects are not laid out in reference order.
    """
    blocks = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S)
    return b"\n".join(b for b in blocks if b"re f" in b)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class AllFamiliesTests(TempCase):
    def test_every_family_generates_a_valid_case(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                g = generate(family, self.out, seed=11, dataset_revision="rev-x")
                self.assertGreater(len(g.case.probes), 0)
                self.assertEqual(check_xref(g.case.pdf_bytes), [])
                self.assertTrue(g.case.pdf_bytes.startswith(b"%PDF-"))
                self.assertTrue(g.case.pdf_bytes.rstrip().endswith(b"%%EOF"))

    def test_ground_truth_roundtrips(self):
        g = generate("pii-packed", self.out, seed=5, dataset_revision="rev-y")
        reloaded = Case.from_dir(self.out / g.case.case_id, dataset_revision="rev-y")
        self.assertEqual(len(reloaded.probes), len(g.case.probes))
        self.assertEqual(reloaded.probes[0], g.case.probes[0])
        self.assertEqual(reloaded.dataset_revision, "rev-y")

    def test_seed_and_stamp_reproduce_byte_identical_pdf(self):
        kw = {"seed": 77, "generated_at": STAMP}
        a = generate("pii-packed", self.out / "a", **kw).case
        b = generate("pii-packed", self.out / "b", **kw).case
        self.assertEqual(a.pdf_bytes, b.pdf_bytes)
        self.assertEqual(a.sha256, b.sha256)

    def test_the_stamp_is_what_makes_it_differ(self):
        """Generation time is on the page, so it has to be pinned to reproduce bytes."""
        a = generate("pii-packed", self.out / "a", seed=77, generated_at=STAMP).case
        b = generate("pii-packed", self.out / "b", seed=77,
                     generated_at="2020-01-01T00:00:00Z").case
        self.assertNotEqual(a.sha256, b.sha256)

    def test_source_date_epoch_pins_the_stamp(self):
        with unittest.mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            a = generate("pii-packed", self.out / "a", seed=78).case
            b = generate("pii-packed", self.out / "b", seed=78).case
        self.assertEqual(a.sha256, b.sha256)

    def test_different_seeds_differ(self):
        a = generate("pii-packed", self.out / "a", seed=1).case
        b = generate("pii-packed", self.out / "b", seed=2).case
        self.assertNotEqual(a.sha256, b.sha256)

    def test_no_lexical_conflicts_in_any_family(self):
        """The scorer hunts each unit across the whole output; overlap breaks scoring."""
        for family in FAMILIES:
            with self.subTest(family=family):
                case = generate(family, self.out / family, seed=3).case
                self.assertEqual(case.lexical_conflicts(), {})

    def test_fiducials_recorded_and_rotationally_unambiguous(self):
        g = generate("pii-packed", self.out, seed=9)
        truth = json.loads((self.out / g.case.case_id / "ground_truth.json").read_text())
        roles = {f["role"]: f for f in truth["fiducials"]}
        self.assertEqual(set(roles), {"tl", "tr", "bl", "br"})
        kinds = [f["kind"] for f in truth["fiducials"]]
        self.assertEqual(kinds.count("donut"), 1, "one corner must differ, or a 180deg "
                                                  "flip is undetectable")

    def test_probe_ids_unique(self):
        case = generate("pii-packed", self.out, seed=4).case
        ids = [p.id for p in case.probes]
        self.assertEqual(len(ids), len(set(ids)))

    def test_distractors_and_targets_both_present(self):
        case = generate("pii-packed", self.out, seed=6).case
        self.assertGreater(len(case.targets), 0)
        self.assertGreater(len(case.distractors), 0, "a page of pure PII rewards "
                                                     "redact-everything")


class BBoxTests(TempCase):
    def _box(self, conditions: Conditions):
        b = CaseBuilder("bbox", 1, "test", PageLayout())
        f = ValueFactory(1)
        probe = b.add_text_probe(f.target("person"), next(b.layout.grid(4)),
                                 conditions=conditions, label=False)
        return probe, probe.bbox

    def test_horizontal_box_matches_courier_metrics(self):
        probe, box = self._box(Conditions())
        expected_w = COURIER.width(probe.value, SCALE_SIZE["pt10"])
        self.assertAlmostEqual(box.x1 - box.x0, expected_w, places=3)

    def test_rot90_box_is_taller_than_wide(self):
        probe, box = self._box(Conditions(orientation="rot90"))
        self.assertGreater(box.y1 - box.y0, box.x1 - box.x0)

    def test_vertical_box_is_taller_than_wide(self):
        probe, box = self._box(Conditions(orientation="vertical"))
        self.assertGreater(box.y1 - box.y0, box.x1 - box.x0)

    def test_vertical_differs_from_rot90(self):
        """Stacked upright glyphs are not a quarter turn, and must not coincide."""
        _, rot = self._box(Conditions(orientation="rot90"))
        _, vert = self._box(Conditions(orientation="vertical"))
        self.assertNotAlmostEqual(rot.y1 - rot.y0, vert.y1 - vert.y0, places=1)

    def test_smaller_scale_gives_smaller_box(self):
        _, big = self._box(Conditions(scale="pt10"))
        _, small = self._box(Conditions(scale="pt4"))
        self.assertLess(small.x1 - small.x0, big.x1 - big.x0)

    def test_all_boxes_are_inside_the_content_area(self):
        """Not merely on the page: the margins hold the fiducials the aligner needs."""
        area = PageLayout().content
        for family in FAMILIES:
            case = generate(family, self.out / family, seed=8).case
            for probe in case.probes:
                if probe.bbox is None:
                    continue
                with self.subTest(family=family, probe=probe.id):
                    self.assertGreaterEqual(probe.bbox.x0, area.x - 1)
                    self.assertGreaterEqual(probe.bbox.y0, area.y - 1)
                    self.assertLessEqual(probe.bbox.x1, area.x + area.width + 1)
                    self.assertLessEqual(probe.bbox.y1,
                                         area.y + area.height + 1)


class ChannelTests(TempCase):
    def test_metadata_probe_is_not_on_the_page(self):
        """If it survives, the tool never opened the metadata channel - so it must not
        be reachable any other way."""
        g = generate("structural-traps", self.out, seed=21)
        pdf = g.case.pdf_bytes
        meta = [p for p in g.case.probes if p.kind is ProbeKind.METADATA_KEY]
        self.assertGreater(len(meta), 0)
        page = content_streams(pdf)
        for probe in meta:
            with self.subTest(probe=probe.id):
                self.assertIn(probe.value.encode("latin-1"), pdf)  # present in the file
                # ...and reachable from nowhere on the page itself
                self.assertNotIn(probe.value.encode("latin-1"), page)

    def test_invisible_probe_uses_render_mode_3(self):
        g = generate("structural-traps", self.out, seed=22)
        self.assertIn(b"3 Tr", g.case.pdf_bytes)
        invisible = [p for p in g.case.probes if p.trap == "invisible_text"]
        # Replicated: one probe per trap cannot separate a failure from a fluke.
        self.assertGreaterEqual(len(invisible), 3)
        for probe in invisible:
            with self.subTest(probe=probe.id):
                self.assertIn(probe.value.encode("latin-1"), g.case.pdf_bytes)

    def test_annotation_probe_lives_outside_the_content_stream(self):
        g = generate("structural-traps", self.out, seed=23)
        pdf = g.case.pdf_bytes
        self.assertIn(b"/FreeText", pdf)
        annot = [p for p in g.case.probes if p.trap == "annotation"][0]
        self.assertIn(annot.value.encode("latin-1"), pdf)

    def test_hidden_layer_is_off_by_default(self):
        g = generate("structural-traps", self.out, seed=24)
        pdf = g.case.pdf_bytes
        self.assertIn(b"/OCProperties", pdf)
        self.assertIn(b"/OFF", pdf)
        self.assertIn(b"/OC /MC0 BDC", pdf)

    def test_prior_revision_holds_the_original(self):
        """The nastiest real case: an earlier revision still carries the secret."""
        g = generate("structural-traps", self.out, seed=25)
        pdf = g.case.pdf_bytes
        probe = [p for p in g.case.probes if p.trap == "prior_revision"][0]
        self.assertEqual(pdf.count(b"%%EOF"), 2, "expected an incremental update")
        self.assertIn(probe.value.encode("latin-1"), pdf)
        self.assertIn(b"/Prev", pdf)
        self.assertEqual(check_xref(pdf), [], "both revisions must have valid xrefs")

    def test_only_one_prior_revision_per_case(self):
        b = CaseBuilder("x", 1, "test", PageLayout())
        f = ValueFactory(1)
        slots = b.layout.grid(4)
        b.add_prior_revision_probe(f.target("person"), next(slots))
        with self.assertRaises(ValueError):
            b.add_prior_revision_probe(f.target("email"), next(slots))


class ConditionMatrixTests(TempCase):
    def test_ofat_and_interaction_counts_match_the_docs(self):
        self.assertEqual(len(OFAT_CELLS), 18)  # 1 control + 4+5+5+2 varied
        # Every axis level appears in the one-factor block, so the block is complete.
        self.assertEqual(len(CONDITION_CELLS),
                         len(OFAT_CELLS) + len(INTERACTION_CELLS))
        self.assertGreaterEqual(len(INTERACTION_CELLS), 20)

    def test_each_ofat_cell_varies_at_most_one_axis(self):
        for cell in OFAT_CELLS:
            with self.subTest(cell=cell):
                self.assertLessEqual(len(cell.varied_axes()), 1)

    def test_each_interaction_cell_varies_more_than_one_axis(self):
        """Pairs, plus a few triples - failures compound."""
        for cell in INTERACTION_CELLS:
            with self.subTest(cell=cell):
                self.assertGreaterEqual(len(cell.varied_axes()), 2)
        self.assertTrue(any(len(c.varied_axes()) == 3 for c in INTERACTION_CELLS),
                        "no three-factor cell in the matrix")

    def test_every_axis_level_appears_somewhere(self):
        from pdfredeval.types import ORIENTATIONS, POLARITIES, PROVENANCES, SCALES

        for axis, levels in (("orientation", ORIENTATIONS), ("polarity", POLARITIES),
                             ("provenance", PROVENANCES), ("scale", SCALES)):
            seen = {getattr(c, axis) for c in CONDITION_CELLS}
            with self.subTest(axis=axis):
                self.assertEqual(seen, set(levels))

    def test_whole_matrix_generates_when_the_backend_is_present(self):
        g = generate("extraction-conditions", self.out, seed=31)
        self.assertEqual(len(g.skipped), 0, [s.reason for s in g.skipped])
        self.assertEqual({p.conditions for p in g.case.probes}, set(CONDITION_CELLS))

    def test_every_cell_carries_the_one_shared_subject(self):
        """One first-and-last name for the whole matrix: a cell measures its condition,
        never which value it drew - and the repeats stay lexically clean."""
        g = generate("extraction-conditions", self.out, seed=31)
        values = {p.value for p in g.case.probes}
        self.assertEqual(len(values), 1, "the matrix must plant exactly one subject")
        first, last = values.pop().split()
        self.assertTrue(first and last, "the subject is a first AND last name")
        self.assertLessEqual(len(first) + len(last) + 1, MATRIX_NAME_GLYPHS)
        self.assertEqual(g.case.lexical_conflicts(), {})

    def test_cells_are_reported_not_dropped_when_the_backend_is_missing(self):
        """Without Pillow the raster cells cannot be drawn - and must say so."""
        with unittest.mock.patch(
            "pdfredeval.generate.builder.have_backend", return_value=False
        ):
            g = generate("extraction-conditions", self.out / "nobackend", seed=31)
        self.assertGreater(len(g.skipped), 0)
        for skip in g.skipped:
            self.assertIn("Pillow", skip.reason)
        covered = {p.conditions for p in g.case.probes}
        skipped = {s.conditions for s in g.skipped}
        self.assertEqual(covered & skipped, set())
        self.assertEqual(len(covered) + len(skipped), len(CONDITION_CELLS))

    def test_screened_watermark_stays_within_its_cell(self):
        """A watermark wider than its probe would condition its neighbours too."""
        from pdfredeval.generate.pdf import COURIER_BOLD

        b = CaseBuilder("wm", 1, "test", PageLayout())
        f = ValueFactory(1)
        grid = Grid(next(b.layout.grid(4)), 1, 24.0)
        probe = b.add_text_probe(
            f.unique(f.short_person), grid.cell(),
            conditions=Conditions(polarity="screened"), label=False)
        stream = b._compose(b._ops).decode("latin-1")
        size = float(stream.split("(CONFIDENTIAL) Tj")[0].rsplit("/F2 ", 1)[1].split()[0])
        width = COURIER_BOLD.width("CONFIDENTIAL", size)
        run_width = probe.bbox.x1 - probe.bbox.x0
        self.assertLessEqual(width, run_width + 6.0,
                             "watermark overruns the probe it belongs to")

    def test_turned_runs_share_one_top_edge(self):
        """Bottom-up and top-down runs must still start level with each other."""
        g = generate("extraction-conditions", self.out, seed=33)
        tall = [p for p in g.case.probes
                if p.conditions.orientation in ("rot90", "rot270", "vertical")]
        self.assertGreaterEqual(len(tall), 4)
        tops = [p.bbox.y1 for p in tall]
        self.assertLess(max(tops) - min(tops), 0.5,
                        f"turned runs are ragged: {sorted(tops)}")

    def test_vertical_and_inverse_are_actually_generated(self):
        g = generate("extraction-conditions", self.out, seed=32)
        orientations = {p.conditions.orientation for p in g.case.probes}
        polarities = {p.conditions.polarity for p in g.case.probes}
        self.assertIn("vertical", orientations)
        self.assertIn("rot90", orientations)
        self.assertIn("inverse", polarities)

    def test_ground_follows_rotated_text(self):
        """A rotated inverse run must sit ON its dark ground, not beside it."""
        import re as _re
        for orientation in ("rot90", "rot270", "vertical"):
            with self.subTest(orientation=orientation):
                b = CaseBuilder("g", 1, "test", PageLayout())
                f = ValueFactory(1)
                probe = b.add_text_probe(
                    f.target("person"), next(b.layout.grid(4)),
                    conditions=Conditions(orientation=orientation, polarity="inverse"),
                    label=False,
                )
                stream = b._compose(b._ops).decode("latin-1")
                rects = [tuple(map(float, m))
                         for m in _re.findall(
                             r"([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re f", stream)]
                dark = rects[-1]  # the ground, painted just before the glyphs
                gx0, gy0, gw, gh = dark
                box = probe.bbox
                self.assertLessEqual(gx0, box.x0 + 0.01)
                self.assertLessEqual(gy0, box.y0 + 0.01)
                self.assertGreaterEqual(gx0 + gw, box.x1 - 0.01)
                self.assertGreaterEqual(gy0 + gh, box.y1 - 0.01)

    def test_inverse_paints_a_dark_ground(self):
        b = CaseBuilder("inv", 1, "test", PageLayout())
        f = ValueFactory(1)
        b.add_text_probe(f.target("person"), next(b.layout.grid(4)),
                         conditions=Conditions(polarity="inverse"), label=False)
        pdf = b.build_pdf()
        self.assertIn(b"0.05 g", pdf)  # dark fill
        self.assertIn(b"1 g", pdf)     # white glyphs


class RenderingStateTests(TempCase):
    """Bugs that are invisible to byte-level checks because the text IS in the file."""

    def test_no_colour_operator_escapes_a_q_Q_pair(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                b, _ = FAMILIES[family](family, 12, dataset_revision=None)
                stream = b._compose(b._ops)
                self.assertEqual(b.check_graphics_state(stream), [])

    def test_probe_text_sets_its_own_fill_colour(self):
        """A run that inherits colour renders in whatever was left behind."""
        b = CaseBuilder("c", 1, "test", PageLayout())
        f = ValueFactory(1)
        probe = b.add_text_probe(f.target("person"), next(b.layout.grid(4)), label=False)
        stream = b._compose(b._ops).decode("latin-1")
        tj = f"({probe.value}) Tj"
        self.assertIn(tj, stream)
        preceding = stream[: stream.index(tj)]
        block = preceding[preceding.rindex("q") :]
        self.assertTrue(
            any(line.endswith(" g") or line.endswith(" rg")
                for line in block.splitlines()),
            f"no fill colour set before the run: {block!r}",
        )

    def test_detects_a_deliberately_leaked_colour(self):
        b = CaseBuilder("c", 1, "test", PageLayout())
        b._ops.append("1 g")  # outside any q/Q
        problems = b.check_graphics_state(b._compose(b._ops))
        self.assertTrue(any("outside q/Q" in p for p in problems), problems)


class RasterConditionTests(TempCase):
    """Probes whose value exists only as pixels."""

    def _case(self, seed: int = 55):
        return generate("extraction-conditions", self.out, seed=seed).case

    def test_raster_probes_are_reachable_only_through_ocr(self):
        case = self._case()
        raster = [p for p in case.probes
                  if p.conditions.provenance in RASTER_CONDITIONS["provenance"]]
        self.assertGreaterEqual(len(raster), 8)
        for probe in raster:
            with self.subTest(probe=probe.id):
                self.assertIs(probe.channel, Channel.IMAGE_TEXT)

    def test_rasterised_cells_add_no_text_to_the_byte_stream(self):
        """One subject, shared: the stream may carry the vector cells' copies, and only
        those. A rasterised cell that also emitted text would not test OCR at all."""
        case = self._case()
        pdf = case.pdf_bytes
        values = {p.value for p in case.probes}
        self.assertEqual(len(values), 1, "the matrix plants exactly one subject")
        subject = values.pop().encode("latin-1")
        # Vertical runs are drawn one glyph at a time, so they never appear as one
        # contiguous string - same convention as test_vector_probes_keep_their_text_layer.
        vector = [p for p in case.probes
                  if p.conditions.provenance not in RASTER_CONDITIONS["provenance"]
                  and p.conditions.orientation != "vertical"]
        self.assertEqual(pdf.count(subject), len(vector))

    def test_vector_probes_keep_their_text_layer(self):
        case = self._case()
        vector = [p for p in case.probes
                  if p.conditions.provenance not in RASTER_CONDITIONS["provenance"]
                  and p.conditions.orientation != "vertical"]
        pdf = case.pdf_bytes
        for probe in vector:
            with self.subTest(probe=probe.id):
                self.assertIn(probe.value.encode("latin-1"), pdf)

    def test_images_are_embedded_and_compressed(self):
        case = self._case()
        pdf = case.pdf_bytes
        raster = [p for p in case.probes
                  if p.conditions.provenance in RASTER_CONDITIONS["provenance"]]
        on_image = [p for p in case.probes if p.conditions.polarity == "on-image"]
        # + 1 for the brand mark in the banner
        self.assertEqual(pdf.count(b"/Subtype /Image"),
                         len(raster) + len(on_image) + 1)
        self.assertIn(b"/Filter /FlateDecode", pdf)

    def test_ground_truth_names_the_glyphs_it_used(self):
        g = generate("extraction-conditions", self.out, seed=56)
        truth = json.loads(
            (self.out / g.case.case_id / "ground_truth.json").read_text())
        fonts = truth["fonts"]
        self.assertIn("RobotoMono.ttf", fonts)
        self.assertIn("Caveat.ttf", fonts)
        for name, digest in fonts.items():
            with self.subTest(font=name):
                self.assertEqual(len(digest), 64)
                self.assertEqual(digest, font_digest(name))

    def test_raster_output_is_deterministic(self):
        kw = {"seed": 57, "generated_at": STAMP}
        a = generate("extraction-conditions", self.out / "a", **kw).case
        b = generate("extraction-conditions", self.out / "b", **kw).case
        self.assertEqual(a.pdf_bytes, b.pdf_bytes)

    def test_hand_mixed_keeps_a_printed_label(self):
        """The condition is a form: printed labels, handwritten values."""
        case = self._case()
        mixed = [p for p in case.probes if p.conditions.provenance == "hand-mixed"]
        self.assertTrue(mixed)
        self.assertIn(b"(Name:) Tj", case.pdf_bytes)

    def test_inverse_is_baked_into_the_glyph_raster(self):
        b = CaseBuilder("inv", 1, "test", PageLayout())
        f = ValueFactory(1)
        probe = b.add_text_probe(
            f.target("person"), next(b.layout.grid(4)),
            conditions=Conditions(polarity="inverse", provenance="hand-block"),
            label=False,
        )
        self.assertIs(probe.channel, Channel.IMAGE_TEXT)
        raster = b._images[-1][1]
        corner = raster.get(0, 0)[0]
        self.assertLess(corner, 60, "an inverse raster must have a dark ground")

    def test_rotated_raster_is_placed_by_matrix(self):
        """A quarter-turned scan is a real condition, not one to refuse."""
        self.assertIsNone(CaseBuilder.check_renderable(
            Conditions(orientation="rot90", provenance="hand-cursive")))
        b = CaseBuilder("rot", 1, "test", PageLayout())
        f = ValueFactory(1)
        slot = next(b.layout.grid(3))
        probe = b.add_text_probe(
            f.target("person"), slot,
            conditions=Conditions(orientation="rot90", provenance="print-clean"),
            label=False)
        self.assertGreater(probe.bbox.y1 - probe.bbox.y0,
                           probe.bbox.x1 - probe.bbox.x0)

    def test_stacked_vertical_raster_is_refused_rather_than_faked(self):
        """One image cannot be one glyph per line; say so instead of drawing a lie."""
        reason = CaseBuilder.check_renderable(
            Conditions(orientation="vertical", provenance="hand-cursive"))
        self.assertIsNotNone(reason)
        self.assertIn("not implemented", reason)


class ProvenanceStampTests(TempCase):
    """The banner travels with the page; the seed must not."""

    def _case(self, family="pii-packed", seed=90):
        return generate(family, self.out, seed=seed, generated_at=STAMP).case

    def test_banner_carries_site_repo_version_and_time(self):
        pdf = self._case().pdf_bytes
        self.assertIn(b"redaction-tools.com", pdf)
        self.assertIn(b"github.com/RedactionTools/pdf-redaction-benchmarks", pdf)
        self.assertIn(b"pdfredeval", pdf)
        self.assertIn(VERSION.encode(), pdf)
        self.assertIn(STAMP.encode(), pdf)

    def test_urls_carry_their_scheme(self):
        """The page gets printed and retyped; a bare host invites a guess."""
        pdf = self._case().pdf_bytes
        self.assertIn(b"https://redaction-tools.com", pdf)
        self.assertIn(b"https://github.com/RedactionTools/pdf-redaction-benchmarks", pdf)

    def test_banner_carries_the_full_wordmark_and_byline(self):
        from pdfredeval.generate.branding import BYLINE, WORDMARK

        pdf = self._case().pdf_bytes
        self.assertIn(f"({WORDMARK}) Tj".encode(), pdf)
        self.assertIn(f"({BYLINE}) Tj".encode(), pdf)

    def test_banner_says_what_the_project_is(self):
        self.assertIn(
            b"Open-source catalog and benchmark of redaction tools",
            self._case().pdf_bytes,
        )

    def test_banner_blocks_never_collide(self):
        """Built from measured text: a longer URL or tagline would overlap silently."""
        from pdfredeval.generate.branding import banner_columns

        left_end, right_start = banner_columns(PageLayout().brand_slot())
        self.assertLess(left_end, right_start - 10.0,
                        "banner text has outgrown the strip")

    def test_banner_content_is_inset_from_the_panel_edges(self):
        from pdfredeval.generate.branding import PAD_X, PROJECT_URL, banner_columns
        from pdfredeval.generate.pdf import COURIER

        slot = PageLayout().brand_slot()
        _, right_start = banner_columns(slot)
        # the widest right-hand line must stop short of the panel edge by the padding
        widest_end = right_start + COURIER.width(PROJECT_URL, 5.6)
        self.assertLessEqual(widest_end, slot.x + slot.width - PAD_X + 0.01)
        self.assertGreater(slot.x + slot.width - widest_end, 1.0)

    def test_repo_link_is_this_tool_not_the_site(self):
        """A disputed page should lead to the generator that produced it."""
        from pdfredeval.generate.branding import REPO_URL

        self.assertTrue(REPO_URL.endswith("/pdf-redaction-benchmarks"))
        self.assertNotIn(b"RedactionTools/redaction-tools)", self._case().pdf_bytes)

    def test_every_family_is_stamped(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                pdf = self._case(family).pdf_bytes
                self.assertIn(b"redaction-tools.com", pdf)
                self.assertIn(STAMP.encode(), pdf)

    def test_seed_is_never_written_into_the_artefact(self):
        """The page goes to the vendor; the seed regenerates a holdout case."""
        case = generate("pii-packed", self.out, seed=987654,
                        case_id="opaque-case", generated_at=STAMP).case
        self.assertNotIn(b"987654", case.pdf_bytes)
        self.assertNotIn(b"seed", case.pdf_bytes.lower())

    def test_metadata_records_provenance(self):
        pdf = self._case().pdf_bytes
        self.assertIn(f"/Producer (pdfredeval {VERSION}".encode(), pdf)
        self.assertIn(b"/CreationDate (D:20260101000000Z)", pdf)
        self.assertIn(b"/ModDate (D:20260101000000Z)", pdf)
        self.assertIn(b"<dc:source>https://github.com/RedactionTools", pdf)

    def test_ground_truth_records_the_stamp_so_it_can_be_reproduced(self):
        g = generate("pii-packed", self.out, seed=91, generated_at=STAMP)
        truth = json.loads(
            (self.out / g.case.case_id / "ground_truth.json").read_text())
        self.assertEqual(truth["generated_at"], STAMP)
        again = generate("pii-packed", self.out / "again", seed=truth["seed"],
                         generated_at=truth["generated_at"]).case
        self.assertEqual(again.sha256, g.case.sha256)

    def test_banner_never_overlaps_a_probe(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                case = self._case(family)
                ceiling = PageLayout().content.y + PageLayout().content.height
                for probe in case.probes:
                    if probe.bbox is None:
                        continue
                    self.assertLessEqual(probe.bbox.y1, ceiling + 1,
                                         f"{probe.id} runs into the banner strip")

    def test_probe_values_cannot_collide_with_banner_text(self):
        """The scorer hunts units across the whole output, banner included."""
        for family in FAMILIES:
            with self.subTest(family=family):
                case = self._case(family)
                stamp_text = " ".join(
                    brand_strings(case.case_id, VERSION, STAMP))
                for probe in case.probes:
                    for unit in probe.units:
                        if len(unit) < 4:
                            continue
                        self.assertNotIn(unit.casefold(), stamp_text.casefold())

    def test_logo_is_the_real_mark_and_is_embedded(self):
        from pdfredeval.generate.branding import LOGO_PATH, logo_raster

        self.assertTrue(LOGO_PATH.exists(), "the vendored logo asset is missing")
        raster = logo_raster()
        self.assertEqual((raster.width, raster.height), (96, 96))

        case = self._case()
        pdf = case.pdf_bytes
        self.assertIn(b"/Subtype /Image", pdf)
        self.assertIn(b"/Width 96", pdf)
        self.assertIn(b"/ColorSpace /DeviceRGB", pdf)

    def test_logo_decodes_without_pillow(self):
        """The brand mark must not depend on the optional imaging extra."""
        import unittest.mock
        with unittest.mock.patch.dict("sys.modules", {"PIL": None}):
            from pdfredeval.generate.branding import logo_raster
            logo_raster.__wrapped__ if hasattr(logo_raster, "__wrapped__") else None
            self.assertEqual(logo_raster(0.5).channels, 3)

    def test_banner_graphics_state_is_balanced(self):
        b = CaseBuilder("logo", 1, "test", PageLayout(), generated_at=STAMP)
        self.assertEqual(b.check_graphics_state(b._compose(b._ops)), [])


class PurposeTests(TempCase):
    """Every page says what it is for, on the page."""

    def test_each_family_states_its_purpose(self):
        from pdfredeval.generate.families import PURPOSES

        for family in FAMILIES:
            with self.subTest(family=family):
                case = generate(family, self.out / family, seed=2,
                                generated_at=STAMP).case
                pdf = case.pdf_bytes
                self.assertIn(family, PURPOSES)
                # Wrapped across lines, so check a distinctive fragment of each line.
                self.assertIn(b"Synthetic benchmark page", pdf)
                self.assertIn(b"Purpose:", pdf)

    def test_purpose_says_the_values_are_invented(self):
        """The page is handed to strangers; it must not read as a real person's data."""
        from pdfredeval.generate.families import PURPOSES

        for family, text in PURPOSES.items():
            with self.subTest(family=family):
                self.assertIn("invented", text)
                self.assertIn("no real person", text)

    def test_purpose_text_cannot_collide_with_probe_values(self):
        from pdfredeval.generate.families import PURPOSES

        for family in FAMILIES:
            with self.subTest(family=family):
                case = generate(family, self.out / f"{family}-c", seed=3,
                                generated_at=STAMP).case
                purpose = PURPOSES[family].casefold()
                for probe in case.probes:
                    for unit in probe.units:
                        if len(unit) >= 4:
                            self.assertNotIn(unit.casefold(), purpose)


class VersionTests(unittest.TestCase):
    """pyproject.toml is the only place a version is written."""

    def test_every_reported_version_is_the_installed_one(self):
        import pdfredeval
        from pdfredeval.generate.builder import GENERATOR_VERSION

        self.assertEqual(pdfredeval.__version__, VERSION)
        self.assertEqual(GENERATOR_VERSION, VERSION)

    def test_no_version_literal_is_hard_coded_in_the_package(self):
        """A copy in the source would drift from pyproject and stamp cases with a lie."""
        import re
        from pathlib import Path

        root = Path(pdfredeval_root())
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "_version.py":
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"""["']\d+\.\d+\.\d+["']""", line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "version literals must come from _version.py")

    def test_generated_case_carries_the_installed_version(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            g = generate("pii-packed", tmp, seed=1, generated_at=STAMP)
            truth = json.loads(
                (Path(tmp) / g.case.case_id / "ground_truth.json").read_text())
        self.assertEqual(truth["generator_version"], VERSION)


def pdfredeval_root() -> str:
    import pdfredeval

    return str(Path(pdfredeval.__file__).parent)


class ValidationTests(TempCase):
    def test_emit_refuses_a_case_with_colliding_units(self):
        b = CaseBuilder("bad", 1, "test", PageLayout())
        f = ValueFactory(1)
        slots = b.layout.grid(4)
        value = f.target("person")
        # The same value again is the sanctioned shared-subject repeat - one subject
        # under several renderings - and must not trip the validator.
        b.add_text_probe(value, next(slots))
        b.add_text_probe(value, next(slots))
        self.assertFalse(any("shared by" in p for p in b.validate()), b.validate())

        # A *different* value reusing its surname is the collision it exists to catch.
        surname = value.units[-1]
        other = Value("EMPLOYER", f"{surname} & Co", (f"{surname} & Co", surname),
                      SPEC["EMPLOYER"], label="Employer")
        b.add_text_probe(other, next(slots))
        problems = b.validate()
        self.assertTrue(any("shared by" in p for p in problems), problems)
        with self.assertRaises(ValueError):
            b.emit(self.out)

    def test_no_probe_box_reaches_into_a_caption(self):
        """The scorer reads a probe box's pixels as the value's.

        A caption inside the box is ink no tool should remove, so a correct redaction
        would score as the value still showing - which is how a fully covered skewed
        name in extraction-conditions came to be reported as a leak.
        """
        for family, build in FAMILIES.items():
            for seed in range(1, 26):
                with self.subTest(family=family, seed=seed):
                    b, skipped = build(f"{family}-{seed:06d}", seed)
                    self.assertEqual(b.check_spatial_isolation(), [])
                    self.assertEqual([s for s in skipped
                                      if s.reason == "no room left on the page"], [])

    def test_a_long_skewed_run_stays_under_its_caption(self):
        b = CaseBuilder("skew", 1, "test", PageLayout())
        f = ValueFactory(1)
        slot = next(b.layout.grid(4))
        value = f.target("person")
        long = Value(value.category, "Bartholomew Featherstonehaugh",
                     ("Bartholomew", "Featherstonehaugh"), value.severity)
        probe = b.add_text_probe(long, slot, conditions=Conditions(orientation="skew"),
                                 label=False)
        caption = next(box for owner, box in b._captions if owner == probe.id)
        self.assertLessEqual(probe.bbox.y1, caption[1] + 1.0)

    def test_emit_refuses_an_empty_case(self):
        b = CaseBuilder("empty", 1, "test", PageLayout())
        with self.assertRaises(ValueError):
            b.emit(self.out)

    def test_checksums_are_valid_so_detectors_do_not_skip_probes(self):
        f = ValueFactory(50)
        card = f.target("card").text
        self.assertEqual(luhn_check_digit(card[:-1]), card[-1])
        iban = f.target("iban").text
        self.assertEqual(iban_check_digits("GB", iban[4:]), iban[2:4])

    def test_pool_exhaustion_is_precise(self):
        f = ValueFactory(60)
        with self.assertRaises(PoolExhausted):
            for _ in range(100):
                f.target("person")

    def test_factory_avoids_reserved_boilerplate(self):
        f = ValueFactory(70)
        f.reserve_text("Kestrelware Holdings")
        for _ in range(10):
            self.assertNotIn("Kestrelware", f.target("person").text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
