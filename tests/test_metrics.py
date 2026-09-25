"""Metric tests, against the formulas and the numbers printed in docs/metrics/core.md.

These are pure functions over the long table, so they are tested on a hand-built frame
rather than on a scored run: the point is whether the arithmetic matches the published
definition, not whether a PDF parsed.
"""

from __future__ import annotations

import math
import unittest

import pandas as pd

from pdfredeval.score import metrics
from pdfredeval.score.rows import Outcome, ProbeRow, to_frame
from pdfredeval.types import BBox, Case, Probe, ProbeKind, Severity


def row(
    probe_id: str,
    outcome: Outcome,
    *,
    must_redact: bool = True,
    severity: str = "high",
    weight: int = 4,
    category: str = "PERSON",
    channel: str = "text_layer",
    leaked: str = "",
    unavailable: str = "",
    ambiguous: bool = False,
    polarity: str = "normal",
    **extra: object,
) -> ProbeRow:
    return ProbeRow(
        run_id="r", case_id="c", probe_id=probe_id, kind="text_span",
        category=category, severity=severity, weight=weight, difficulty="easy",
        must_redact=must_redact, ambiguous=ambiguous, outcome=outcome.value,
        removed=outcome in (Outcome.TP, Outcome.FP), scope="in_scope",
        channel=channel, leaked_layers=leaked, unavailable_layers=unavailable,
        complete=not unavailable, polarity=polarity,
        **extra,  # type: ignore[arg-type]
    )


class WilsonTests(unittest.TestCase):
    def test_zero_leaks_in_forty_probes_is_not_certainty(self):
        """The published example: `[0, 0.088]`, not "100%"."""
        low, high = metrics.wilson(0, 40)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.088, places=3)

    def test_the_interval_is_defined_at_both_extremes(self):
        for successes in (0, 40):
            low, high = metrics.wilson(successes, 40)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 1.0)

    def test_no_trials_is_undefined_rather_than_zero(self):
        rate = metrics.rate(0, 0)
        self.assertFalse(rate.defined)
        self.assertIsNone(rate.to_dict()["value"])

    def test_more_evidence_narrows_the_interval(self):
        narrow = metrics.wilson(0, 400)
        wide = metrics.wilson(0, 40)
        self.assertLess(narrow[1], wide[1])


class RateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = to_frame([
            row("a", Outcome.FN),
            row("b", Outcome.TP),
            row("c", Outcome.FP, must_redact=False),
            row("d", Outcome.TN, must_redact=False),
            row("e", Outcome.UNSUPPORTED),
        ])

    def test_leak_rate_ignores_the_distractors(self):
        rate = metrics.leak_rate(self.frame)
        self.assertEqual(rate.numerator, 1)
        self.assertEqual(rate.denominator, 2)

    def test_over_redaction_rate_ignores_the_targets(self):
        rate = metrics.over_redaction_rate(self.frame)
        self.assertEqual((rate.numerator, rate.denominator), (1, 2))

    def test_unsupported_probes_enter_no_rate(self):
        """A text-only tool is scored on text and marked silent on faces."""
        self.assertEqual(metrics.leak_rate(self.frame).denominator, 2)

    def test_ambiguous_probes_enter_no_rate(self):
        frame = to_frame([
            row("a", Outcome.FN),
            row("x", Outcome.FP, must_redact=False, ambiguous=True),
        ])
        self.assertEqual(metrics.over_redaction_rate(frame).denominator, 0)

    def test_an_empty_frame_is_undefined_everywhere(self):
        empty = to_frame([])
        self.assertFalse(metrics.leak_rate(empty).defined)
        self.assertTrue(math.isnan(metrics.weighted_leak_rate(empty)))


class WeightedLeakTests(unittest.TestCase):
    def test_one_critical_leak_outweighs_a_run_of_low_ones(self):
        """Weights double per level, so a critical leak is eight low ones."""
        frame = to_frame([
            row("crit", Outcome.FN, severity="critical", weight=8),
            *[row(f"low{i}", Outcome.TP, severity="low", weight=1) for i in range(7)],
        ])
        self.assertAlmostEqual(metrics.weighted_leak_rate(frame), 8 / 15)
        self.assertGreater(metrics.weighted_leak_rate(frame), 0.5)


class RqsTests(unittest.TestCase):
    def test_redact_everything_scores_zero(self):
        """Utility 0: blacks out the page, useless document."""
        self.assertEqual(metrics.rqs(utility=0.0, privacy=1.0), 0.0)

    def test_do_nothing_scores_zero(self):
        """Privacy 0: returns the input untouched."""
        self.assertEqual(metrics.rqs(utility=1.0, privacy=0.0), 0.0)

    def test_a_perfect_tool_scores_one(self):
        self.assertAlmostEqual(metrics.rqs(utility=1.0, privacy=1.0), 1.0)

    def test_beta_tilts_toward_privacy(self):
        """The trade-off is in the open, as a number someone can argue with."""
        privacy_heavy = metrics.rqs(utility=1.0, privacy=0.5, beta=2.0)
        utility_heavy = metrics.rqs(utility=0.5, privacy=1.0, beta=2.0)
        self.assertGreater(utility_heavy, privacy_heavy)

    def test_undefined_inputs_stay_undefined(self):
        self.assertTrue(math.isnan(metrics.rqs(float("nan"), 1.0)))


class BreakdownTests(unittest.TestCase):
    def test_per_category_split_is_available(self):
        """"94% overall" hides "0% on IBANs"."""
        frame = to_frame([
            row("a", Outcome.FN, category="IBAN"),
            row("b", Outcome.TP, category="PERSON"),
            row("c", Outcome.TP, category="PERSON"),
        ])
        split = metrics.by(frame, "category")
        self.assertEqual(split["IBAN"]["leak_rate"]["value"], 1.0)
        self.assertEqual(split["PERSON"]["leak_rate"]["value"], 0.0)

    def test_reach_is_the_redaction_rate_per_channel(self):
        """`Reach_k = 0` across a channel is a single-sentence finding."""
        frame = to_frame([
            row("a", Outcome.FN, channel="metadata"),
            row("b", Outcome.FN, channel="metadata"),
            row("c", Outcome.TP, channel="text_layer"),
        ])
        reach = metrics.reach(frame)
        self.assertEqual(reach["metadata"]["value"], 0.0)
        self.assertEqual(reach["text_layer"]["value"], 1.0)

    def test_condition_cost_is_measured_against_the_control(self):
        """`Delta` isolates the condition from the tool's baseline ability."""
        frame = to_frame([
            row("a", Outcome.TP, polarity="normal"),
            row("b", Outcome.TP, polarity="normal"),
            row("c", Outcome.FN, polarity="inverse"),
            row("d", Outcome.FN, polarity="inverse"),
        ])
        cost = metrics.condition_cost(frame, "polarity")
        self.assertEqual(cost["normal"], 0.0)
        self.assertEqual(cost["inverse"], 1.0)

    def test_condition_cost_needs_a_control(self):
        frame = to_frame([row("c", Outcome.FN, polarity="inverse")])
        self.assertEqual(metrics.condition_cost(frame, "polarity"), {})


class LayerRateTests(unittest.TestCase):
    def test_a_layer_that_catches_nothing_new_has_no_exclusive_rate(self):
        """Exactly what justifies retiring a layer from the suite."""
        frame = to_frame([
            row("a", Outcome.FN, leaked="content_stream,ocr"),
            row("b", Outcome.FN, leaked="content_stream,ocr"),
        ])
        rates = metrics.layer_rates(frame, ["content_stream", "ocr"])
        self.assertEqual(rates["ocr"]["layer_leak_rate"]["value"], 1.0)
        self.assertEqual(rates["ocr"]["exclusive_leak_rate"]["value"], 0.0)

    def test_a_layer_that_alone_catches_a_leak_earns_its_place(self):
        frame = to_frame([
            row("a", Outcome.FN, leaked="prior_revision"),
            row("b", Outcome.TP),
        ])
        rates = metrics.layer_rates(frame, ["prior_revision"])
        self.assertEqual(rates["prior_revision"]["exclusive_leak_rate"]["value"], 0.5)

    def test_unread_layers_are_counted_not_hidden(self):
        frame = to_frame([row("a", Outcome.TP, unavailable="ocr")])
        rates = metrics.layer_rates(frame, ["ocr"])
        self.assertEqual(rates["ocr"]["unavailable"], 1)


class CollateralTests(unittest.TestCase):
    def _case(self) -> Case:
        return Case(
            case_id="c", pdf_path="x.pdf",
            probes=(
                Probe(id="t", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                      value="target", bbox=BBox(0, 100, 100, 200, 112)),
                Probe(id="near", kind=ProbeKind.TEXT_SPAN, must_redact=False,
                      value="neighbour", severity=Severity.LOW,
                      bbox=BBox(0, 100, 88, 200, 99)),
                Probe(id="far", kind=ProbeKind.TEXT_SPAN, must_redact=False,
                      value="distant", severity=Severity.LOW,
                      bbox=BBox(0, 100, 400, 200, 412)),
            ),
        )

    def test_a_blunt_brush_is_told_apart_from_a_loose_detector(self):
        frame = to_frame([
            row("t", Outcome.TP),
            row("near", Outcome.FP, must_redact=False),
            row("far", Outcome.TN, must_redact=False),
        ])
        rate = metrics.collateral(frame, self._case())
        self.assertEqual((rate.numerator, rate.denominator), (1, 2))

    def test_a_distant_over_redaction_is_not_collateral(self):
        frame = to_frame([
            row("t", Outcome.TP),
            row("near", Outcome.TN, must_redact=False),
            row("far", Outcome.FP, must_redact=False),
        ])
        self.assertEqual(metrics.collateral(frame, self._case()).numerator, 0)


class PoolingTests(unittest.TestCase):
    def test_counts_pool_and_rates_never_average(self):
        """Averaging per-case rates over-weights the cases with fewest probes."""
        small = to_frame([row("a", Outcome.FN)])
        large = to_frame([row(f"b{i}", Outcome.TP) for i in range(9)])
        pooled = metrics.pooled_leak_rate([small, large])
        self.assertEqual((pooled.numerator, pooled.denominator), (1, 10))
        self.assertNotEqual(pooled.value, (1.0 + 0.0) / 2)

    def test_instability_needs_more_than_one_attempt(self):
        frame = to_frame([row("a", Outcome.FN)])
        self.assertTrue(math.isnan(metrics.instability([frame])))

    def test_instability_counts_probes_that_change_their_mind(self):
        """A tool at LR 0.1 with instability 0.3 is a coin toss with a good average."""
        first = to_frame([row("a", Outcome.FN), row("b", Outcome.TP)])
        second = to_frame([row("a", Outcome.TP), row("b", Outcome.TP)])
        self.assertEqual(metrics.instability([first, second]), 0.5)

    def test_bootstrap_needs_more_than_one_page(self):
        frame = to_frame([row("a", Outcome.FN)])
        low, high = metrics.bootstrap_pages([frame])
        self.assertTrue(math.isnan(low))

    def test_bootstrap_over_pages_brackets_the_pooled_rate(self):
        """Probes on one page share a fate, so the resampling unit is the page."""
        pages = [
            to_frame([row(f"p{p}_{i}", Outcome.FN if p == 0 else Outcome.TP)
                      for i in range(5)])
            for p in range(4)
        ]
        low, high = metrics.bootstrap_pages(pages, draws=300, seed=1)
        pooled = metrics.pooled_leak_rate(pages).value
        self.assertLessEqual(low, pooled)
        self.assertGreaterEqual(high, pooled)


class AgreementTests(unittest.TestCase):
    def test_disagreements_are_listed_not_scored(self):
        frame = to_frame([
            row("amb", Outcome.FP, must_redact=False, ambiguous=True),
            row("t", Outcome.TP),
        ])
        agreement = metrics.agreement(frame)
        self.assertEqual(agreement["n"], 1)
        self.assertEqual(agreement["rate"], 0.0)
        self.assertEqual(agreement["disagreements"][0]["probe_id"], "amb")

    def test_no_ambiguous_probes(self):
        self.assertEqual(metrics.agreement(to_frame([row("t", Outcome.TP)]))["n"], 0)


class FrameTests(unittest.TestCase):
    def test_an_empty_table_still_has_its_columns(self):
        frame = to_frame([])
        self.assertIsInstance(frame, pd.DataFrame)
        for column in ("probe_id", "outcome", "leaked_layers"):
            self.assertIn(column, frame.columns)
