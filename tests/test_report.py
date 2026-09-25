"""Reporter tests.

Two properties carry most of the weight here. **Counts pool; rates never average** - a
comparison that got that wrong would rank tools by how many probes their case happened
to carry. And **the page is self-contained** - a result that renders as broken boxes six
months later in someone else's mail client is not evidence of anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

import fakes

from pdfredeval import report as reporter
from pdfredeval.capabilities import Capabilities
from pdfredeval.generate import generate
from pdfredeval.manifest import RunManifest
from pdfredeval.report import charts, html, model, overlay, tables
from pdfredeval.report.palette import DARK, LIGHT, STATUS
from pdfredeval.score import score
from pdfredeval.thresholds import DEFAULTS
from pdfredeval.types import Case, Transport

THRESHOLDS = DEFAULTS.but(dpi=110)


def _write_run(
    root: Path, case: Case, run_id: str, tool_id: str, output: bytes,
    *, tier: str | None = None, capabilities: Capabilities | None = None,
) -> Path:
    run = root / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "output.pdf").write_bytes(output)
    manifest = RunManifest(
        run_id=run_id, case_id=case.case_id, tool_id=tool_id,
        transport=Transport.MANUAL, tier=tier, dataset_revision="rev-test",
        input_sha256=case.sha256, output_sha256=hashlib.sha256(output).hexdigest(),
        capabilities=(capabilities or Capabilities(
            categories=frozenset(), channels=frozenset(),
        )).to_dict(),
    )
    manifest.save(run / "manifest.json")
    score(case, output, manifest=manifest, thresholds=THRESHOLDS,
          ocr_enabled=False).write(run / "score")
    return run


class ReportBase(unittest.TestCase):
    """Three fake tools, scored once. Every test here reads those artefacts back."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.case = generate("pii-detection", cls.dir / "cases", seed=13).case
        cls.runs = cls.dir / "runs"
        cls.leaky = _write_run(cls.runs, cls.case, "leaky-1", "acme:web",
                               fakes.do_nothing(cls.case), tier="free")
        cls.clean = _write_run(cls.runs, cls.case, "clean-1", "borealis:api",
                               fakes.true_redaction(cls.case), tier="paid")
        cls.blunt = _write_run(cls.runs, cls.case, "blunt-1", "cindr:desktop",
                               fakes.rasterise(cls.case, dpi=110), tier="trial")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def one(self, path: Path) -> model.Report:
        return reporter.load([path])

    def all_three(self) -> model.Report:
        return reporter.load([self.runs])


class LoadingTests(ReportBase):
    def test_loads_a_run_directory(self):
        loaded = self.one(self.leaky)
        self.assertEqual(len(loaded.runs), 1)
        self.assertEqual(loaded.runs[0].tool_id, "acme:web")
        self.assertEqual(loaded.runs[0].label, "acme:web (free)")

    def test_loads_a_score_directory_directly(self):
        loaded = reporter.load([self.leaky / "score"])
        self.assertEqual(loaded.runs[0].case_id, self.case.case_id)

    def test_a_parent_directory_takes_every_run_under_it(self):
        self.assertEqual(len(self.all_three().runs), 3)

    def test_an_unscored_run_says_how_to_score_it(self):
        empty = self.dir / "runs" / "never-scored"
        empty.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(model.NothingToReport) as caught:
            reporter.load([empty])
        self.assertIn("pdfredeval score", str(caught.exception))

    def test_a_report_needs_a_run(self):
        with self.assertRaises(model.NothingToReport):
            model.Report(())


class PoolingTests(ReportBase):
    def test_counts_pool_rather_than_rates_averaging(self):
        """Averaging 1.0, 0.0 and 0.98 would give 0.66 only by coincidence here."""
        pooled = self.all_three().headline()
        leak = pooled["leak_rate"]
        singles = [self.one(p).headline()["leak_rate"] for p in
                   (self.leaky, self.clean, self.blunt)]
        self.assertEqual(leak.numerator, sum(s.numerator for s in singles))
        self.assertEqual(leak.denominator, sum(s.denominator for s in singles))

    def test_pages_are_published_beside_probes(self):
        head = self.all_three().headline()
        self.assertEqual(head["pages"], 3)
        self.assertEqual(head["probes"], len(self.case.probes) * 3)

    def test_more_than_one_page_gets_a_bootstrap_interval(self):
        """Probes on one page share a fate, so pages are the resampling unit."""
        self.assertIsNotNone(self.all_three().headline()["bootstrap"])
        self.assertIsNone(self.one(self.leaky).headline()["bootstrap"])

    def test_a_single_run_reproduces_its_own_score(self):
        loaded = self.one(self.leaky)
        written = json.loads((self.leaky / "score" / "report.json").read_text())
        self.assertEqual(
            loaded.headline()["leak_rate"].value,
            written["summary"]["leak_rate"]["value"],
        )

    def test_tools_are_ranked_by_leak_rate_then_over_redaction(self):
        rows = tables.comparison(self.all_three()).rows
        self.assertEqual(rows[0][0], "borealis:api (paid)", "the clean tool leads")
        self.assertEqual(rows[-1][0], "acme:web (free)", "the do-nothing tool trails")

    def test_repeats_of_one_case_measure_instability(self):
        repeat = _write_run(self.dir / "repeat", self.case, "leaky-2", "acme:web",
                            fakes.black_box(self.case), tier="free")
        loaded = reporter.load([self.leaky, repeat])
        self.assertIsNotNone(loaded.headline()["instability"])


class HonestyTests(ReportBase):
    def test_runs_scored_under_different_tables_are_flagged(self):
        other = self.dir / "other"
        other.mkdir(exist_ok=True)
        run = _write_run(other, self.case, "strict-1", "acme:web",
                         fakes.do_nothing(self.case))
        # Re-score the same bytes under a different threshold table.
        score(self.case, fakes.do_nothing(self.case),
              thresholds=THRESHOLDS.but(tau_cov=0.5), ocr_enabled=False,
              ).write(run / "score")
        loaded = reporter.load([self.leaky, run])
        self.assertTrue(loaded.mixed_thresholds)
        self.assertTrue(any("threshold" in n for n in loaded.notes()))

    def test_a_declared_gap_is_published_not_swallowed(self):
        narrow = self.dir / "narrow"
        narrow.mkdir(exist_ok=True)
        run = _write_run(narrow, self.case, "narrow-1", "acme:web",
                         fakes.do_nothing(self.case),
                         capabilities=Capabilities(categories=frozenset({"PERSON"})))
        gaps = reporter.load([run]).coverage_gaps()
        self.assertTrue(gaps)
        self.assertTrue(any("does not attempt" in g for g in gaps))

    def test_an_unrestricted_scope_claims_no_gap(self):
        """An empty declared set means no limit, not "attempts nothing"."""
        gaps = self.one(self.leaky).coverage_gaps()
        self.assertFalse(any("channels" in g for g in gaps))

    def test_an_unread_layer_reaches_the_coverage_table(self):
        table = tables.coverage(self.one(self.leaky))
        self.assertTrue(any("ocr" in row[1] for row in table.rows))


class TableTests(ReportBase):
    def test_a_rate_is_never_printed_without_its_interval_and_n(self):
        cell = tables.fmt_rate(self.one(self.leaky).headline()["leak_rate"])
        self.assertRegex(cell, r"^\d\.\d+ \[\d\.\d+, \d\.\d+\] n=\d+$")

    def test_an_undefined_number_is_not_a_zero(self):
        self.assertEqual(tables.fmt(None), "n/a")
        self.assertEqual(tables.fmt(float("nan")), "n/a")
        self.assertEqual(tables.fmt_rate(None), "n/a")

    def test_the_per_category_breakdown_is_present(self):
        table = tables.by_category(self.one(self.leaky))
        self.assertFalse(table.empty)
        self.assertIn("PERSON", [row[0] for row in table.rows])

    def test_the_outcome_table_reconciles_with_its_own_denominators(self):
        table = tables.counts(self.all_three())
        total = sum(int(row[1]) for row in table.rows)
        self.assertEqual(total, self.all_three().headline()["probes"])

    def test_markdown_renders_a_table_per_section(self):
        text = tables.markdown_report(self.one(self.leaky))
        self.assertIn("# ", text)
        self.assertIn("| Metric |", text)
        self.assertIn("Leak rate (LR)", text)

    def test_a_pipe_in_a_value_does_not_break_the_markdown(self):
        table = tables.Table("k", "T", ("A",), (("a|b",),))
        self.assertIn(r"a\|b", tables.to_markdown(table))

    def test_an_empty_table_renders_as_nothing(self):
        table = tables.Table("k", "T", ("A",), ())
        self.assertEqual(tables.to_markdown(table), "")
        self.assertEqual(tables.to_text(table), "")

    def test_the_terminal_summary_stays_short(self):
        text = reporter.terminal_summary(self.one(self.leaky))
        self.assertIn("Headline", text)
        self.assertNotIn("Provenance", text, "the long tables belong in the file")

    def test_provenance_names_the_threshold_table(self):
        rows = tables.provenance(self.one(self.leaky)).rows
        self.assertTrue(any(row[0] == "thresholds" and row[1] == "tau_cov"
                            for row in rows))


class ChartTests(ReportBase):
    def test_every_chart_renders_in_both_modes(self):
        loaded = self.all_three()
        for mode in (LIGHT, DARK):
            figures = charts.render_all(loaded, mode)
            self.assertEqual(set(figures), set(charts.CHARTS))
            for key, figure in figures.items():
                with self.subTest(mode=mode.name, chart=key):
                    self.assertTrue(figure.svg or figure.reason)

    def test_a_chart_with_nothing_to_plot_gives_a_reason_not_a_blank_box(self):
        figure = charts.condition_cost(self.one(self.leaky), LIGHT)
        self.assertIsNone(figure.svg)
        self.assertIn("control", figure.reason)

    def test_the_same_numbers_produce_the_same_svg(self):
        """No timestamp in the output: a report diffs, like everything else here."""
        loaded = self.one(self.leaky)
        first = charts.leak_by_category(loaded, LIGHT).svg
        second = charts.leak_by_category(loaded, LIGHT).svg
        self.assertEqual(first, second)

    def test_the_two_modes_are_different_renders(self):
        loaded = self.one(self.leaky)
        self.assertNotEqual(
            charts.leak_by_category(loaded, LIGHT).svg,
            charts.leak_by_category(loaded, DARK).svg,
        )

    def test_the_svg_carries_no_xml_prologue(self):
        """It is inlined into an HTML document, where a second prologue is invalid."""
        svg = charts.leak_by_category(self.one(self.leaky), LIGHT).svg
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<?xml", svg)

    def test_a_series_colour_is_never_a_status_colour(self):
        """Status is reserved: a hue that means 'bad' must not also mean 'series 1'."""
        for mode in (LIGHT, DARK):
            with self.subTest(mode=mode.name):
                self.assertNotIn(mode.series.lower(),
                                 [c.lower() for c in STATUS.values()])


class PageTests(ReportBase):
    def test_the_page_is_self_contained(self):
        """No CDN, no font server, no sibling directory - it gets forwarded."""
        page = html.render(self.all_three())
        for fetched in (r'src="http', r'href="http', "<link", "@import", "url(http"):
            with self.subTest(pattern=fetched):
                self.assertNotIn(fetched, page)
        self.assertNotIn("<script", page, "a page of evidence runs nothing")

    def test_both_themes_ship_in_one_file(self):
        page = html.render(self.one(self.leaky))
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn('data-theme="dark"', page)
        self.assertIn("only-light", page)
        self.assertIn("only-dark", page)

    def test_the_hero_is_the_leak_rate(self):
        page = html.render(self.one(self.leaky))
        self.assertIn('class="figure">1.000<', page)

    def test_a_failed_gate_is_flagged_on_the_page(self):
        page = html.render(self.one(self.blunt))
        self.assertIn("gate failed: not_rasterised", page)

    def test_markup_is_escaped(self):
        table = tables.Table("k", "T", ("A",), (("<script>x</script>",),))
        self.assertNotIn("<script>", html.table_html(table))

    def test_a_status_cell_carries_a_glyph_beside_the_colour(self):
        table = tables.Table("k", "T", ("A",), (("x",),), roles=(("critical",),))
        rendered = html.table_html(table)
        self.assertIn("var(--critical)", rendered)
        self.assertIn("!", rendered, "colour is never the only channel")

    def test_every_chart_has_a_sentence_saying_what_it_is_for(self):
        for key in charts.CHARTS:
            with self.subTest(chart=key):
                self.assertIn(key, html.CHART_NOTES)
                self.assertGreater(len(html.CHART_NOTES[key]), 40)


class OverlayTests(ReportBase):
    def test_draws_one_overlay_for_a_run(self):
        out = self.dir / "overlays"
        written = reporter.render_overlays(
            self.one(self.leaky), out, case_dir=self.dir / "cases" / self.case.case_id,
        )
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].name.endswith("-overlay.png"))
        self.assertTrue(all(p.exists() and p.stat().st_size > 0 for p in written))

    def test_only_the_outcomes_in_question_are_named(self):
        """A caption on all eighty-six probes buries the handful worth looking at."""
        self.assertIn("FN", overlay.LABELLED)
        self.assertIn("FP", overlay.LABELLED)
        self.assertNotIn("TP", overlay.LABELLED)
        self.assertNotIn("TN", overlay.LABELLED)

    def test_a_missing_case_is_skipped_rather_than_raised(self):
        written = reporter.render_overlays(
            self.one(self.leaky), self.dir / "nowhere",
            cases_root=self.dir / "no-cases-here",
        )
        self.assertEqual(written, [])


class WriteTests(ReportBase):
    def test_writes_markdown_and_a_page(self):
        out = self.dir / "written"
        result = reporter.write(self.one(self.leaky), out)
        self.assertTrue(result.markdown.exists())
        self.assertTrue(result.page.exists())
        self.assertIn("Leak rate", result.markdown.read_text())

    def test_the_header_counts_passed_and_failed_probes(self):
        report = self.one(self.leaky)
        head = report.headline()
        counts = head["counts"]
        self.assertEqual(head["passed"], counts["TP"] + counts["TN"])
        self.assertEqual(head["failed"], counts["FN"] + counts["FP"])
        self.assertEqual(head["passed"] + head["failed"] + head["not_scored"],
                         head["probes"])
        verdicts = f"{head['passed']} passed · {head['failed']} failed"
        self.assertIn(verdicts, tables.markdown_report(report).split("### ")[0])
        self.assertIn(f"{head['failed']} failed", html.render(report))

    def test_either_output_can_be_turned_off(self):
        out = self.dir / "page-only"
        result = reporter.write(self.one(self.leaky), out, markdown=False)
        self.assertIsNone(result.markdown)
        self.assertTrue(result.page.exists())

    def test_an_embedded_image_is_inlined_not_linked(self):
        out = self.dir / "with-images"
        images = reporter.render_overlays(
            self.one(self.leaky), out / "overlay",
            case_dir=self.dir / "cases" / self.case.case_id,
        )
        page = html.render(self.one(self.leaky), images=images)
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn(images[0].name + '"', page.replace("data:", ""))


class PaletteTests(unittest.TestCase):
    def test_both_modes_define_every_role(self):
        for mode in (LIGHT, DARK):
            for field in ("surface", "ink", "series", "ramp_strong", "ramp_weak",
                          "diverge_low", "diverge_high", "grid"):
                with self.subTest(mode=mode.name, field=field):
                    self.assertRegex(getattr(mode, field), r"^#[0-9a-f]{6}$")

    def test_dark_is_a_selected_set_not_an_inverted_light_one(self):
        self.assertNotEqual(LIGHT.series, DARK.series)
        self.assertNotEqual(LIGHT.ramp_strong, DARK.ramp_strong)

    def test_an_outcome_carries_a_glyph_as_well_as_a_colour(self):
        from pdfredeval.report.palette import outcome_colour, outcome_glyph

        for outcome in ("FN", "TP", "FP", "TN", "undecided"):
            with self.subTest(outcome=outcome):
                self.assertTrue(outcome_glyph(outcome))
                self.assertRegex(outcome_colour(outcome, LIGHT), r"^#[0-9a-f]{6}$")


class MarkdownShapeTests(ReportBase):
    def test_the_document_has_one_title_and_no_stray_pipes(self):
        text = tables.markdown_report(self.all_three())
        self.assertEqual(len(re.findall(r"^# ", text, re.M)), 1)
        for line in text.splitlines():
            if line.startswith("|"):
                self.assertTrue(line.endswith("|"), line)
