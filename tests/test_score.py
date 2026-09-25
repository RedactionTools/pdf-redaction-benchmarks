"""Scorer tests: known tools, known answers.

Every case here is a failure mode the benchmark exists to tell apart. A scorer that
cannot separate "covered the pixels" from "removed the value" is measuring nothing, and
the pairing of `black_box` with `true_redaction` is the test that says it can.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fakes
from surfaces import leak_surface_case

from pdfredeval.capabilities import Capabilities
from pdfredeval.generate import generate
from pdfredeval.manifest import RunManifest
from pdfredeval.probe.base import Layer
from pdfredeval.score import Outcome, score, score_run
from pdfredeval.score.detection import Entity
from pdfredeval.score.detection import report as detection_report
from pdfredeval.score.rules import Verdict
from pdfredeval.score.scorer import ScoringError
from pdfredeval.thresholds import DEFAULTS
from pdfredeval.types import Case, Stage, Transport

THRESHOLDS = DEFAULTS.but(dpi=120)


def score_fake(case: Case, output: bytes, **kwargs: object):
    kwargs.setdefault("thresholds", THRESHOLDS)
    kwargs.setdefault("ocr_enabled", False)
    return score(case, output, **kwargs)  # type: ignore[arg-type]


class ScoreBase(unittest.TestCase):
    case: Case

    FAMILY = "pii-detection"
    SEED = 7

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.case = generate(cls.FAMILY, cls.dir, seed=cls.SEED).case

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


class KnownToolTests(ScoreBase):
    def test_do_nothing_leaks_everything_and_over_redacts_nothing(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        summary = result.summary()
        self.assertEqual(summary["leak_rate"]["value"], 1.0)
        self.assertEqual(summary["over_redaction_rate"]["value"], 0.0)
        self.assertEqual(summary["counts"]["TP"], 0)
        self.assertEqual(summary["privacy"], 0.0)

    def test_true_redaction_leaks_nothing_and_keeps_the_document(self):
        result = score_fake(self.case, fakes.true_redaction(self.case))
        summary = result.summary()
        self.assertEqual(summary["leak_rate"]["value"], 0.0)
        self.assertEqual(summary["over_redaction_rate"]["value"], 0.0)
        self.assertTrue(result.observations.survivability.passed)
        self.assertEqual(summary["utility"], 1.0)

    def test_a_black_box_over_live_text_is_not_a_redaction(self):
        """It looks redacted, it prints redacted, and pdftotext returns the name."""
        result = score_fake(self.case, fakes.black_box(self.case))
        self.assertEqual(result.summary()["leak_rate"]["value"], 1.0)

        # Text-layer probes leak through the stream the cover did not touch. The
        # page's metadata probes leak too, but through a surface a rectangle was never
        # going to reach.
        frame = result.frame()
        leaked = frame[(frame["outcome"] == Outcome.FN.value)
                       & (frame["channel"] == "text_layer")]
        self.assertGreater(len(leaked), 0)
        self.assertTrue(
            all(Layer.CONTENT_STREAM.value in s for s in leaked["leaked_layers"])
        )

    def test_the_content_stream_is_what_catches_the_black_box(self):
        """Exclusive Leak Rate is what justifies keeping a layer in the suite."""
        result = score_fake(self.case, fakes.black_box(self.case))
        layers = result.report()["layers"]
        exclusive = layers[Layer.CONTENT_STREAM.value]["exclusive_leak_rate"]["value"]
        self.assertGreater(exclusive, 0.5)
        self.assertEqual(
            layers[Layer.RENDERED_PIXELS.value]["layer_leak_rate"]["value"], 0.0,
            "the cover did its visual job; only the parser sees the leak",
        )

    def test_flattening_the_page_is_published_with_its_cost(self):
        """`LR` alone would reward destroying the document."""
        result = score_fake(self.case, fakes.rasterise(self.case, dpi=120))
        summary = result.summary()
        self.assertEqual(summary["text_retention"], 0.0)
        failures = result.observations.survivability.failures
        self.assertIn("not_rasterised", failures)
        self.assertIn("text_retained", failures)

    def test_a_turned_page_scores_the_same_as_an_upright_one(self):
        flat = score_fake(self.case, fakes.rasterise(self.case, dpi=120)).frame()
        turned = score_fake(self.case, fakes.rotate(self.case, 90, dpi=120)).frame()
        self.assertEqual(list(flat["outcome"]), list(turned["outcome"]))

    def test_a_lost_registration_mark_withholds_the_raster_verdict(self):
        result = score_fake(self.case, fakes.crop_corner(self.case, dpi=120))
        self.assertFalse(result.observations.alignment.confident)
        self.assertIn(Layer.RENDERED_PIXELS.value, result.observations.unavailable_layers)

        frame = result.frame()
        self.assertTrue((~frame["complete"]).any(), "an unread layer must mark the row")
        self.assertTrue(any("could not be read" in n or "marks" in n
                            for n in result.notes))

    def test_an_unparsable_output_scores_rather_than_raises(self):
        result = score_fake(self.case, fakes.broken_pdf(self.case))
        self.assertFalse(result.observations.survivability.passed)
        self.assertGreater(len(result.rows), 0)

    def test_an_output_nobody_can_open_gets_no_privacy_rate(self):
        """The failure this whole design exists to avoid: a flattering LR of zero."""
        result = score_fake(self.case, fakes.broken_pdf(self.case))
        summary = result.summary()
        self.assertIsNone(summary["leak_rate"]["value"])
        self.assertEqual(summary["counts"]["TP"], 0)
        self.assertEqual(summary["counts"][Outcome.UNDECIDED.value], len(result.rows))

    def test_an_undecided_probe_is_not_a_removed_one(self):
        result = score_fake(self.case, fakes.broken_pdf(self.case))
        self.assertTrue(all(not row.removed for row in result.rows))


class LayerAttributionTests(ScoreBase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.case = leak_surface_case(cls.dir, seed=11)

    def test_every_planted_surface_is_named_by_the_layer_that_caught_it(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        caught = {
            row.probe_id: set(filter(None, row.leaked_layers.split(",")))
            for row in result.rows
        }
        expected = {
            "invisible_text": Layer.CONTENT_STREAM.value,
            "annotation": Layer.ANNOTATION.value,
            "optional_content": Layer.OPTIONAL_CONTENT.value,
            "prior_revision": Layer.PRIOR_REVISION.value,
            "info_dict": Layer.METADATA.value,
            "xmp": Layer.METADATA.value,
        }
        for item in self.case.probes:
            if item.trap in expected:
                with self.subTest(probe=item.id, trap=item.trap):
                    self.assertIn(expected[item.trap], caught[item.id])

    def test_a_probe_with_no_visible_ink_is_not_a_pixel_leak(self):
        """Invisible text draws nothing, so coverage of its box means nothing."""
        result = score_fake(self.case, fakes.do_nothing(self.case))
        invisible = [p for p in self.case.probes if p.trap == "invisible_text"][0]
        verdict = result.layer_results[invisible.id][Layer.RENDERED_PIXELS]
        self.assertIs(verdict.verdict, Verdict.NOT_APPLICABLE)
        self.assertIn("nothing is drawn", verdict.reason)

    def test_a_prior_revision_box_describes_the_placeholder(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        revision = [p for p in self.case.probes if p.trap == "prior_revision"][0]
        verdict = result.layer_results[revision.id][Layer.RENDERED_PIXELS]
        self.assertIs(verdict.verdict, Verdict.NOT_APPLICABLE)

    def test_rewriting_the_file_removes_the_earlier_generation(self):
        """Which is a real fix, and the scorer must credit it."""
        result = score_fake(self.case, fakes.true_redaction(self.case))
        revision = [p for p in self.case.probes if p.trap == "prior_revision"][0]
        row = next(r for r in result.rows if r.probe_id == revision.id)
        self.assertEqual(row.outcome, Outcome.TP.value)


class ScopeTests(ScoreBase):
    def test_probes_outside_the_declared_scope_are_not_misses(self):
        text_only = Capabilities(
            stages=frozenset({Stage.REDACTION}),
            categories=frozenset({"PERSON"}),
        )
        result = score_fake(self.case, fakes.do_nothing(self.case),
                            capabilities=text_only)
        frame = result.frame()
        unsupported = frame[frame["outcome"] == Outcome.UNSUPPORTED.value]
        self.assertGreater(len(unsupported), 0)
        self.assertTrue(all(r for r in unsupported["unsupported_reason"]))

        report = result.report()
        self.assertEqual(report["unsupported"]["n"], len(unsupported))
        self.assertTrue(report["unsupported"]["reasons"], "an exclusion must be named")
        self.assertEqual(sum(report["summary"]["counts"].values()),
                         report["summary"]["probes"])

    def test_an_exclusion_does_not_flatter_the_leak_rate(self):
        """A text-only tool is scored on text, not failed - and not credited either."""
        narrow = Capabilities(categories=frozenset({"PERSON"}))
        result = score_fake(self.case, fakes.do_nothing(self.case), capabilities=narrow)
        self.assertEqual(result.summary()["leak_rate"]["value"], 1.0)

    def test_a_run_without_capabilities_scores_everything_and_says_so(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        self.assertTrue(any("declares no capabilities" in n for n in result.notes))
        frame = result.frame()
        self.assertEqual((frame["outcome"] == Outcome.UNSUPPORTED.value).sum(), 0)


class AmbiguousTests(ScoreBase):
    def test_arguable_probes_are_held_out_and_reported(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        report = result.report()
        self.assertGreater(report["ambiguous"]["n"], 0,
                           "pii-detection plants an employer name, which is arguable")
        counts = report["summary"]["counts"]
        self.assertEqual(
            sum(counts.values()), report["summary"]["probes"],
            "the outcome table must reconcile with its own denominators",
        )


class DetectionTests(ScoreBase):
    def test_no_entity_list_means_inferred_mode(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        detection = result.report()["detection"]
        self.assertEqual(detection["mode"], "inferred")
        self.assertIn("unidentifiable", detection["note"])

    def test_a_reported_entity_list_is_matched_against_the_truth(self):
        targets = self.case.targets[:5]
        entities = [Entity(category=p.category, text=p.value) for p in targets]
        report = detection_report(entities, self.case)
        self.assertEqual(report.mode, "observed")
        self.assertEqual(report.matched, len(targets))
        self.assertEqual(report.precision, 1.0)
        self.assertEqual(report.type_accuracy, 1.0)

    def test_a_mislabelled_entity_is_a_type_error_not_a_miss(self):
        target = self.case.targets[0]
        report = detection_report([Entity(category="ORG", text=target.value)], self.case)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.type_accuracy, 0.0)
        self.assertIn(target.category, report.confusion)

    def test_matching_is_one_to_one(self):
        target = self.case.targets[0]
        entities = [Entity(category=target.category, text=target.value)] * 3
        report = detection_report(entities, self.case)
        self.assertEqual(report.matched, 1)
        self.assertAlmostEqual(report.precision, 1 / 3, places=5)


class RunDirectoryTests(ScoreBase):
    def _run_dir(self, output: bytes, *, manifest_changes: dict | None = None) -> Path:
        run = self.dir / "runs" / "r1"
        run.mkdir(parents=True, exist_ok=True)
        (run / "output.pdf").write_bytes(output)
        import hashlib

        manifest = RunManifest(
            run_id="r1", case_id=self.case.case_id, tool_id="demo:web",
            transport=Transport.MANUAL, dataset_revision="rev-1",
            input_sha256=self.case.sha256,
            output_sha256=hashlib.sha256(output).hexdigest(),
            capabilities=Capabilities().to_dict(),
        )
        for key, value in (manifest_changes or {}).items():
            setattr(manifest, key, value)
        manifest.save(run / "manifest.json")
        return run

    def test_scores_a_collected_run_and_writes_its_artefacts(self):
        run = self._run_dir(fakes.true_redaction(self.case))
        result = score_run(run, case_dir=self.dir / self.case.case_id,
                           thresholds=THRESHOLDS, ocr_enabled=False)
        out = result.write(run / "score")
        self.assertTrue((out / "probes.csv").exists())
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["case_id"], self.case.case_id)
        self.assertEqual(report["manifest"]["run_id"], "r1")
        self.assertIn("thresholds", report, "a score names the table it was computed on")

    def test_declared_capabilities_reach_the_scorer_from_the_manifest(self):
        """Scoring happens days later, in a process that never saw the adapter."""
        run = self._run_dir(fakes.do_nothing(self.case))
        result = score_run(run, case_dir=self.dir / self.case.case_id,
                           thresholds=THRESHOLDS, ocr_enabled=False)
        frame = result.frame()
        self.assertGreater((frame["outcome"] == Outcome.UNSUPPORTED.value).sum(), 0)

    def test_output_bytes_are_checked_against_the_manifest(self):
        run = self._run_dir(fakes.do_nothing(self.case),
                            manifest_changes={"output_sha256": "0" * 64})
        with self.assertRaises(ScoringError) as caught:
            score_run(run, case_dir=self.dir / self.case.case_id, thresholds=THRESHOLDS)
        self.assertIn("does not match the manifest", str(caught.exception))

    def test_the_case_is_checked_too(self):
        run = self._run_dir(fakes.do_nothing(self.case),
                            manifest_changes={"input_sha256": "0" * 64})
        with self.assertRaises(ScoringError):
            score_run(run, case_dir=self.dir / self.case.case_id, thresholds=THRESHOLDS)

    def test_verification_can_be_waived_deliberately(self):
        run = self._run_dir(fakes.do_nothing(self.case),
                            manifest_changes={"output_sha256": "0" * 64})
        result = score_run(run, case_dir=self.dir / self.case.case_id,
                           thresholds=THRESHOLDS, ocr_enabled=False, verify=False)
        self.assertGreater(len(result.rows), 0)

    def test_a_missing_case_says_how_to_get_it_back(self):
        run = self._run_dir(fakes.do_nothing(self.case))
        with self.assertRaises(ScoringError) as caught:
            score_run(run, cases_root=self.dir / "nowhere", thresholds=THRESHOLDS)
        self.assertIn("generate", str(caught.exception))

    def test_an_uncollected_run_is_refused(self):
        empty = self.dir / "runs" / "empty"
        empty.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ScoringError):
            score_run(empty, thresholds=THRESHOLDS)


class RowTests(ScoreBase):
    def test_one_row_per_probe_with_the_published_columns(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        frame = result.frame()
        self.assertEqual(len(frame), len(self.case.probes))
        for column in ("run_id", "case_id", "probe_id", "kind", "category", "severity",
                       "difficulty", "must_redact", "outcome", "evidence"):
            self.assertIn(column, frame.columns)

    def test_conditions_are_flat_columns_so_metrics_are_groupbys(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        frame = result.frame()
        for axis in ("orientation", "polarity", "provenance", "scale"):
            self.assertIn(axis, frame.columns)

    def test_evidence_survives_as_json(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        frame = result.frame()
        parsed = json.loads(frame["evidence"].iloc[0])
        self.assertIn(Layer.CONTENT_STREAM.value, parsed)

    def test_a_leak_is_graded_not_merely_flagged(self):
        result = score_fake(self.case, fakes.do_nothing(self.case))
        frame = result.frame()
        leaked = frame[frame["outcome"] == Outcome.FN.value]
        self.assertTrue(set(leaked["grade"]) <= {"full", "partial"})


class RepeatedValueTests(ScoreBase):
    """`extraction-conditions` draws one value under every condition on one page."""

    FAMILY = "extraction-conditions"
    SEED = 1

    def test_one_surviving_copy_leaks_one_probe_not_all(self):
        """Judged page-wide, the one copy left behind is a leak on every probe."""
        result = score_fake(self.case, fakes.true_redaction(self.case, survivors=1))
        leaked = [row for row in result.rows
                  if Layer.CONTENT_STREAM.value in row.leaked_layers]
        self.assertEqual(len(leaked), 1, [row.probe_id for row in leaked])
        evidence = result.layer_results[leaked[0].probe_id][Layer.CONTENT_STREAM].evidence
        self.assertEqual(evidence["scope"], "probe")

    def test_text_outside_every_probe_box_still_counts(self):
        """Moved rather than deleted is still a leak: unplaced text reaches every probe."""
        result = score_fake(self.case, fakes.do_nothing(self.case))
        for row in result.rows:
            if row.must_redact and row.channel == "text_layer":
                self.assertIn(Layer.CONTENT_STREAM.value, row.leaked_layers, row.probe_id)
