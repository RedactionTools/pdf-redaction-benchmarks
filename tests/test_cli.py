"""CLI tests. Exercised through main(), so argument wiring is covered too."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pdfredeval.capabilities import Capabilities
from pdfredeval.cli import main, parse_seeds
from pdfredeval.tools import ManualTool, registry


class DemoWeb(ManualTool):
    tool_id = "demo:web"
    url = "https://demo.example/redact"
    instructions = "Pick 'Full redaction'."

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(categories=frozenset({"PERSON", "EMAIL"}))


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SeedParsingTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_seeds("7"), [7])
        self.assertEqual(parse_seeds("1,2,3"), [1, 2, 3])
        self.assertEqual(parse_seeds("1-4"), [1, 2, 3, 4])
        self.assertEqual(parse_seeds("1-3,9"), [1, 2, 3, 9])
        self.assertEqual(parse_seeds(" 5 , 6 "), [5, 6])

    def test_negative_seed_is_not_a_range(self):
        self.assertEqual(parse_seeds("-5"), [-5])

    def test_rejects_empty_and_backwards(self):
        for bad in ("", "  ", ",,"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_seeds(bad)
        with self.assertRaises(ValueError):
            parse_seeds("9-2")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        self._tmp.cleanup()
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_families_lists_all(self):
        code, out, _ = run("families")
        self.assertEqual(code, 0)
        self.assertIn("pii-packed", out)
        self.assertIn("extraction-conditions", out)

    def test_generate_writes_case_and_truth(self):
        code, out, _ = run("generate", "pii-packed", "-s", "42", "-o", str(self.out))
        self.assertEqual(code, 0)
        case_dir = self.out / "pii-packed-000042"
        self.assertTrue((case_dir / "pii-packed-000042.pdf").exists())
        self.assertFalse((case_dir / "case.pdf").exists())
        self.assertTrue((case_dir / "ground_truth.json").exists())
        self.assertRegex(out, r"\d+ probes")

    def test_generate_accepts_a_seed_range(self):
        code, out, _ = run("generate", "pii-packed", "-s", "1-3", "-o", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 3)
        for seed in (1, 2, 3):
            case_id = f"pii-packed-{seed:06d}"
            self.assertTrue((self.out / case_id / f"{case_id}.pdf").exists())

    def test_generate_reports_skipped_cells_on_stderr(self):
        import unittest.mock
        with unittest.mock.patch(
            "pdfredeval.generate.builder.have_backend", return_value=False
        ):
            code, out, err = run("generate", "extraction-conditions", "-s", "7",
                                 "-o", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Pillow", err)
        self.assertNotIn("skipped", out, "skips belong on stderr, not in the case list")

    def test_full_matrix_has_nothing_to_skip(self):
        code, _, err = run("generate", "extraction-conditions", "-s", "7",
                           "-o", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(err.strip(), "")

    def test_generate_rejects_case_id_with_multiple_seeds(self):
        code, _, err = run("generate", "pii-packed", "-s", "1-2", "-o", str(self.out),
                           "--case-id", "fixed")
        self.assertEqual(code, 1)
        self.assertIn("--case-id", err)

    def test_unknown_family_is_a_clean_error_not_a_traceback(self):
        code, _, err = run("generate", "nope", "-s", "1", "-o", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("nope", err)
        self.assertNotIn("Traceback", err)

    def test_inspect_summarises(self):
        run("generate", "structural-traps", "-s", "5", "-o", str(self.out),
            "--dataset-revision", "rev-9")
        code, out, _ = run("inspect", str(self.out / "structural-traps-000005"))
        self.assertEqual(code, 0)
        self.assertIn("structural-traps", out)
        self.assertIn("rev-9", out)
        self.assertIn("conflicts     0", out)

    def test_inspect_probes_lists_traps(self):
        run("generate", "structural-traps", "-s", "5", "-o", str(self.out))
        code, out, _ = run("inspect", str(self.out / "structural-traps-000005"), "--probes")
        self.assertEqual(code, 0)
        self.assertIn("trap=invisible_text", out)
        self.assertIn("trap=prior_revision", out)

    def test_tools_reports_an_empty_registry_as_a_failure(self):
        code, _, err = run("tools")
        self.assertEqual(code, 1)
        self.assertIn("entry-point", err)

    def test_tools_lists_registered_adapters(self):
        registry.register(DemoWeb, replace=True)
        code, out, _ = run("tools")
        self.assertEqual(code, 0)
        self.assertIn("demo:web", out)

    def test_submit_then_collect_round_trip(self):
        registry.register(DemoWeb, replace=True)
        run("generate", "pii-packed", "-s", "3", "-o", str(self.out))
        case_dir = self.out / "pii-packed-000003"
        runs = self.out / "runs"

        code, out, _ = run("submit", "demo:web", str(case_dir),
                           "--runs-dir", str(runs), "--operator", "mm",
                           "--profile", '{"redact": ["PERSON"]}',
                           "--dataset-revision", "rev-3")
        self.assertEqual(code, 0)
        self.assertIn("TASK.md", out)
        run_dir = next(runs.iterdir())
        self.assertTrue((run_dir / "TASK.md").exists())

        # nothing delivered yet
        code, _, err = run("collect", str(run_dir))
        self.assertEqual(code, 1)
        self.assertIn("has not delivered", err)
        self.assertNotIn("Traceback", err)

        (run_dir / "output.pdf").write_bytes(b"%PDF-1.7\nhand redacted\n%%EOF\n")
        code, out, _ = run("collect", str(run_dir))
        self.assertEqual(code, 0)
        self.assertIn("complete      True", out)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["tool_id"], "demo:web")
        self.assertEqual(manifest["operator"], "mm")
        self.assertEqual(manifest["profile"], {"redact": ["PERSON"]})

    def test_run_identity_survives_a_fresh_process_at_collect(self):
        """submit and collect are days and processes apart on the manual path."""
        registry.register(DemoWeb, replace=True)
        run("generate", "pii-packed", "-s", "4", "-o", str(self.out))
        runs = self.out / "runs"
        run("submit", "demo:web", str(self.out / "pii-packed-000004"),
            "--runs-dir", str(runs), "--operator", "alex", "--tier", "free",
            "--dataset-revision", "rev-4")
        run_dir = next(runs.iterdir())
        (run_dir / "output.pdf").write_bytes(b"%PDF-1.7\nx\n%%EOF\n")

        # collect knows nothing but the run directory
        code, _, _ = run("collect", str(run_dir))
        self.assertEqual(code, 0)
        m = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(m["operator"], "alex")
        self.assertEqual(m["tier"], "free")
        self.assertEqual(m["dataset_revision"], "rev-4")
        self.assertEqual(m["url"], "https://demo.example/redact")

    def test_collect_can_fill_a_field_submission_left_unset(self):
        registry.register(DemoWeb, replace=True)
        run("generate", "pii-packed", "-s", "4", "-o", str(self.out))
        runs = self.out / "runs"
        run("submit", "demo:web", str(self.out / "pii-packed-000004"),
            "--runs-dir", str(runs), "--dataset-revision", "rev-4")
        run_dir = next(runs.iterdir())
        (run_dir / "output.pdf").write_bytes(b"%PDF-1.7\nx\n%%EOF\n")
        run("collect", str(run_dir), "--operator", "late")
        m = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(m["operator"], "late")

    def test_submit_without_an_adapter_lays_out_a_manual_run(self):
        run("generate", "pii-packed", "-s", "3", "-o", str(self.out))
        runs = self.out / "runs"
        code, out, err = run("submit", "ghost:api", str(self.out / "pii-packed-000003"),
                             "--runs-dir", str(runs), "--dataset-revision", "rev-3")
        self.assertEqual(code, 0, err)
        self.assertIn("no adapter for 'ghost:api'", err)
        run_dir = next(runs.iterdir())
        self.assertTrue((run_dir / "screenshots").is_dir())
        self.assertIn("pii-packed-000003.pdf", (run_dir / "TASK.md").read_text())

        # collect needs no adapter either, and the run keeps the id it was given
        (run_dir / "pii-packed-000003.pdf").write_bytes(b"%PDF-1.7\nx\n%%EOF\n")
        code, _, err = run("collect", str(run_dir))
        self.assertEqual(code, 0, err)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["tool_id"], "ghost:api")
        self.assertEqual(manifest["transport"], "manual")
        # Nothing is known about the tool, so nothing is excused as out of scope.
        from pdfredeval.capabilities import Capabilities
        from pdfredeval.types import Case
        claimed = Capabilities.from_dict(manifest["capabilities"])
        case = Case.from_dir(self.out / "pii-packed-000003")
        self.assertEqual([p.id for p in case.probes if not claimed.supports(p)], [])

    def test_submit_with_an_invalid_tool_id_is_a_clean_error(self):
        run("generate", "pii-packed", "-s", "3", "-o", str(self.out))
        code, _, err = run("submit", "ghost", str(self.out / "pii-packed-000003"),
                           "--runs-dir", str(self.out / "runs"))
        self.assertEqual(code, 1)
        self.assertIn("vendor:surface", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.out / "runs").exists())

    def test_profile_can_come_from_a_file(self):
        registry.register(DemoWeb, replace=True)
        run("generate", "pii-packed", "-s", "3", "-o", str(self.out))
        profile = self.out / "p.json"
        profile.write_text('{"mode": "strict"}')
        runs = self.out / "runs"
        code, _, _ = run("submit", "demo:web", str(self.out / "pii-packed-000003"),
                         "--runs-dir", str(runs), "--profile", f"@{profile}")
        self.assertEqual(code, 0)
        handle = json.loads((next(runs.iterdir()) / "handle.json").read_text())
        self.assertEqual(handle["extra"]["profile"], {"mode": "strict"})


class ScoreCommandTests(unittest.TestCase):
    """`score` is the one command that needs the score extra, so it imports lazily."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        run("generate", "redaction-layers", "-s", "5", "-o", str(self.out))
        self.case_dir = self.out / "redaction-layers-000005"
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        self._tmp.cleanup()
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_scoring_a_pdf_against_its_case_needs_no_adapter(self):
        """The input scored against itself is the do-nothing tool: everything leaks."""
        code, out, _ = run("score", str(self.case_dir / "redaction-layers-000005.pdf"),
                           "--case-dir", str(self.case_dir), "--dpi", "100", "--no-ocr",
                           "--no-report")
        self.assertEqual(code, 0)
        self.assertIn("leak rate     1", out)
        self.assertIn("alignment     fiducial", out)

    def test_it_writes_the_probe_table_and_the_report(self):
        run("score", str(self.case_dir / "redaction-layers-000005.pdf"),
            "--case-dir", str(self.case_dir), "--dpi", "100", "--no-ocr",
            "--out", str(self.out / "score"), "--no-report")
        rows = (self.out / "score" / "probes.csv").read_text().splitlines()
        self.assertGreater(len(rows), 2, "one header and one row per probe")
        report = json.loads((self.out / "score" / "report.json").read_text())
        self.assertIn("thresholds", report)
        self.assertEqual(report["case_id"], "redaction-layers-000005")

    def test_json_prints_the_whole_report_and_writes_nothing(self):
        code, out, _ = run("score", str(self.case_dir / "redaction-layers-000005.pdf"),
                           "--case-dir", str(self.case_dir), "--dpi", "100",
                           "--no-ocr", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn("summary", report)
        self.assertFalse((self.case_dir / "score").exists())

    def test_no_report_stops_after_the_score_directory(self):
        code, _, _ = run("score", str(self.case_dir / "redaction-layers-000005.pdf"),
                         "--case-dir", str(self.case_dir), "--dpi", "100", "--no-ocr",
                         "--no-report")
        self.assertEqual(code, 0)
        self.assertTrue((self.case_dir / "score" / "probes.csv").exists())
        self.assertFalse((self.case_dir / "report").exists())

    def test_a_skipped_ocr_layer_is_reported_unavailable_not_clean(self):
        code, _, err = run("score", str(self.case_dir / "redaction-layers-000005.pdf"),
                           "--case-dir", str(self.case_dir), "--dpi", "100", "--no-ocr",
                           "--no-report")
        self.assertEqual(code, 0)
        self.assertIn("ocr", err)

    def test_a_bare_pdf_without_a_case_is_a_clean_error(self):
        code, _, err = run("score", str(self.case_dir / "redaction-layers-000005.pdf"))
        self.assertEqual(code, 1)
        self.assertIn("--case-dir", err)
        self.assertNotIn("Traceback", err)

    def test_an_uncollected_run_directory_is_a_clean_error(self):
        empty = self.out / "runs" / "nothing"
        empty.mkdir(parents=True)
        code, _, err = run("score", str(empty))
        self.assertEqual(code, 1)
        self.assertIn("collect", err)
        self.assertNotIn("Traceback", err)

    def test_scoring_again_reports_from_the_artefacts_without_rescoring(self):
        registry.register(DemoWeb, replace=True)
        runs = self.out / "runs"
        run("submit", "demo:web", str(self.case_dir), "--runs-dir", str(runs))
        run_dir = next(runs.iterdir())
        output = run_dir / "redaction-layers-000005__demo-web.pdf"
        shutil.copyfile(self.case_dir / "redaction-layers-000005.pdf", output)
        run("collect", str(run_dir))

        code, _, _ = run("score", str(run_dir), "--cases-dir", str(self.out),
                         "--dpi", "100", "--no-ocr")
        self.assertEqual(code, 0)

        # A re-score would die on the missing output; the artefacts carry it instead.
        output.unlink()
        code, out, _ = run("score", str(run_dir), "--cases-dir", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("--rescore to score it again", out)
        self.assertTrue((run_dir / "report" / "report.html").exists())


class ScoreReportingTests(unittest.TestCase):
    """`score` reports what it scored; the separate `report` command is gone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        run("generate", "redaction-layers", "-s", "8", "-o", str(self.out))
        self.case_dir = self.out / "redaction-layers-000008"
        self.pdf = str(self.case_dir / "redaction-layers-000008.pdf")
        self.common = ["--case-dir", str(self.case_dir), "--dpi", "100", "--no-ocr"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_report_command_is_gone(self):
        with self.assertRaises(SystemExit):
            run("report", str(self.out))

    def test_it_reports_what_it_scored(self):
        code, out, _ = run("score", self.pdf, *self.common)
        self.assertEqual(code, 0)
        self.assertIn("leak rate", out)
        self.assertTrue((self.case_dir / "report" / "report.md").exists())
        self.assertTrue((self.case_dir / "report" / "report.html").exists())

    def test_a_title_reaches_the_page(self):
        run("score", self.pdf, *self.common, "--title", "Acme, October")
        self.assertIn("Acme, October",
                      (self.case_dir / "report" / "report.html").read_text())

    def test_a_parent_directory_pools_its_runs(self):
        runs = self.out / "runs"
        for name in ("one", "two"):
            run("score", self.pdf, *self.common, "--out", str(runs / name / "score"))
        # A pooled report belongs to no one run, so it lands in ./report of the cwd.
        cwd = os.getcwd()
        os.chdir(self.out)
        try:
            code, out, _ = run("score", str(runs))
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertIn("Tools compared", out)
        self.assertTrue((self.out / "report" / "report.html").exists())

    def test_an_empty_directory_is_a_clean_error(self):
        empty = self.out / "nothing"
        empty.mkdir()
        code, _, err = run("score", str(empty))
        self.assertEqual(code, 1)
        self.assertIn("collect", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
