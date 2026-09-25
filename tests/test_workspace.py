"""The versioned benchmark tree: where things land, and what is never overwritten unasked."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_cli import run

from pdfredeval import __version__
from pdfredeval.capabilities import Capabilities
from pdfredeval.errors import BenchmarkError
from pdfredeval.generate import FAMILIES
from pdfredeval.tools import ManualTool, registry
from pdfredeval.types import Case
from pdfredeval.workspace import (
    Overwrite,
    Workspace,
    confirm_overwrite,
    family_of,
    output_pdf_name,
    resolve_case,
    version_tag,
)

VERSION = version_tag(__version__)


class DemoWeb(ManualTool):
    tool_id = "demo:web"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()


class OtherApi(DemoWeb):
    tool_id = "other:api"


class NamingTests(unittest.TestCase):
    def test_every_family_is_recovered_from_its_case_ids(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(family_of(f"{family}-000123"), family)
        self.assertIsNone(family_of("adhoc"))

    def test_names_say_what_the_file_is(self):
        self.assertEqual(output_pdf_name("pii-packed-000001"), "pii-packed-000001.pdf")
        self.assertEqual(version_tag("0.1.1"), "v0.1.1")
        self.assertEqual(version_tag("v0.1.1"), "v0.1.1")

    def test_a_case_is_found_nested_by_family_or_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "pii-packed" / "pii-packed-000001"
            nested.mkdir(parents=True)
            (nested / "ground_truth.json").write_text("{}")
            self.assertEqual(resolve_case("pii-packed-000001", root), nested)

            flat = root / "structural-traps-000002"
            flat.mkdir()
            (flat / "ground_truth.json").write_text("{}")
            self.assertEqual(resolve_case("structural-traps-000002", root), flat)

    def test_a_legacy_case_pdf_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            run("generate", "pii-packed", "-s", "1", "-o", tmp)
            case_dir = Path(tmp) / "pii-packed-000001"
            (case_dir / "pii-packed-000001.pdf").rename(case_dir / "case.pdf")
            truth = json.loads((case_dir / "ground_truth.json").read_text())
            truth.pop("pdf")
            (case_dir / "ground_truth.json").write_text(json.dumps(truth))
            self.assertEqual(Case.from_dir(case_dir).pdf_path.name, "case.pdf")


class ConfirmTests(unittest.TestCase):
    existing = [Path("a"), Path("b")]

    def ask(self, answer: str, interactive: bool = True) -> bool:
        with mock.patch("pdfredeval.workspace._interactive", return_value=interactive), \
                mock.patch("sys.stdin", io.StringIO(answer)), \
                mock.patch("sys.stderr", io.StringIO()):
            return confirm_overwrite(self.existing, "cases", Overwrite.ASK)

    def test_flags_decide_without_asking(self):
        self.assertTrue(confirm_overwrite(self.existing, "cases", Overwrite.FORCE))
        self.assertFalse(confirm_overwrite(self.existing, "cases", Overwrite.SKIP))
        self.assertTrue(confirm_overwrite([], "cases", Overwrite.ASK))

    def test_a_person_is_asked(self):
        self.assertTrue(self.ask("o\n"))
        self.assertFalse(self.ask("s\n"))
        self.assertFalse(self.ask("what\ns\n"))
        with self.assertRaises(BenchmarkError):
            self.ask("a\n")
        with self.assertRaises(BenchmarkError):
            self.ask("")  # EOF is not consent

    def test_nobody_at_a_terminal_must_say_which(self):
        with self.assertRaisesRegex(BenchmarkError, "--force"):
            self.ask("o\n", interactive=False)


class TreeTests(unittest.TestCase):
    """The commands with no output flags, against $PDFREDEVAL_HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "benchmarks"
        self._env = mock.patch.dict(os.environ, {"PDFREDEVAL_HOME": str(self.root)})
        self._env.start()
        self._tty = mock.patch("pdfredeval.workspace._interactive", return_value=False)
        self._tty.start()
        self.ws = Workspace(self.root, VERSION)
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        self._tty.stop()
        self._env.stop()
        self._tmp.cleanup()
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_generate_all_fills_every_family_of_this_version(self):
        code, _, err = run("generate", "all", "-s", "1")
        self.assertEqual(code, 0, err)
        for family in FAMILIES:
            case_dir = self.ws.cases_dir(family) / f"{family}-000001"
            self.assertTrue((case_dir / f"{family}-000001.pdf").exists())
            truth = json.loads((case_dir / "ground_truth.json").read_text())
            self.assertEqual(truth["dataset_revision"], VERSION)
        self.assertEqual((self.root / "LATEST").read_text().strip(), VERSION)
        index = (self.ws.home / "README.md").read_text()
        self.assertIn("| [pii-packed](cases/pii-packed/) | 1 |", index)

    def test_existing_cases_are_never_overwritten_unasked(self):
        run("generate", "pii-packed", "-s", "1-2")
        pdf = self.ws.cases_dir("pii-packed") / "pii-packed-000001" / "pii-packed-000001.pdf"
        os.utime(pdf, (1, 1))

        code, _, err = run("generate", "pii-packed", "-s", "1-3")
        self.assertEqual(code, 1)
        self.assertIn("2 cases already exist", err)
        self.assertFalse((pdf.parent.parent / "pii-packed-000003").exists())

        code, out, _ = run("generate", "pii-packed", "-s", "1-3", "--skip-existing")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(pdf.stat().st_mtime, 1)

        code, _, _ = run("generate", "pii-packed", "-s", "1", "--force")
        self.assertEqual(code, 0)
        self.assertNotEqual(pdf.stat().st_mtime, 1)

    def _collected_run(self, tool: type[ManualTool], case_id: str) -> Path:
        registry.register(tool, replace=True)
        code, _, err = run("submit", tool.tool_id, case_id)
        self.assertEqual(code, 0, err)
        run_dir = next(self.ws.runs_dir(tool.tool_id).iterdir())
        shutil.copyfile(Case.from_dir(resolve_case(case_id, self.ws.cases_root)).pdf_path,
                        run_dir / "downloaded from the vendor (1).pdf")
        code, _, err = run("collect", str(run_dir))
        self.assertEqual(code, 0, err)
        return run_dir

    def test_runs_scores_and_reports_land_per_tool_and_family(self):
        run("generate", "redaction-layers", "-s", "5")
        demo = self._collected_run(DemoWeb, "redaction-layers-000005")
        self._collected_run(OtherApi, "redaction-layers-000005")
        manifest = json.loads((demo / "manifest.json").read_text())
        # Delivered under the vendor's own name; the manifest pins which file it was.
        self.assertEqual(manifest["output_name"], "downloaded from the vendor (1).pdf")
        self.assertIn("redaction-layers-000005.pdf", (demo / "TASK.md").read_text())

        code, _, err = run("score", "--dpi", "100", "--no-ocr")
        self.assertEqual(code, 0, err)
        self.assertTrue((demo / "score" / "report.json").exists())
        reports = self.ws.reports_root
        for tool in ("demo-web", "other-api", "_summary"):
            with self.subTest(tool=tool):
                self.assertTrue((reports / tool / "report.html").exists())
                self.assertTrue((reports / tool / "redaction-layers" / "report.md").exists())
        # Overlays by default, drawn once per run: in its tool-and-family report only.
        drawn = sorted(p.relative_to(reports) for p in reports.rglob("*-overlay.png"))
        self.assertEqual([str(p.parent) for p in drawn], [
            "demo-web/redaction-layers/overlay", "other-api/redaction-layers/overlay",
        ])
        # ...and shown on every page that includes the run, summary included.
        for page in reports.rglob("report.html"):
            with self.subTest(page=str(page.relative_to(reports))):
                shots = page.read_text().count('class="shot"')
                self.assertEqual(shots, 2 if "_summary" in page.parts else 1)
        index = (self.ws.home / "README.md").read_text()
        self.assertIn("| [demo-web](runs/demo-web/) | 1 | 1 |", index)
        self.assertIn("reports/_summary/report.md", index)

        # Scored runs are read back; re-scoring must be asked for, and confirmed.
        code, out, _ = run("score")
        self.assertEqual(code, 0)
        self.assertIn("already scored", out)
        self.assertIn("up to date", out)
        # Rescoring one run by name updates its score/; the next sweep must notice.
        code, _, err = run("score", str(demo), "--rescore", "--force", "--dpi", "100",
                           "--no-ocr", "--no-report")
        self.assertEqual(code, 0, err)
        code, out, _ = run("score")
        self.assertNotIn("up to date", out)
        self.assertIn("reports/demo-web/report.html", out)

        code, _, err = run("score", "--rescore")
        self.assertEqual(code, 1)
        self.assertIn("2 scores already exist", err)
        code, _, err = run("score", "--rescore", "--force", "--dpi", "100", "--no-ocr",
                           "--no-report")
        self.assertEqual(code, 0, err)

    def test_a_legacy_output_name_still_scores(self):
        run("generate", "redaction-layers", "-s", "5")
        registry.register(DemoWeb, replace=True)
        run("submit", "demo:web", "redaction-layers-000005")
        run_dir = next(self.ws.runs_dir("demo:web").iterdir())
        case_pdf = self.ws.cases_dir("redaction-layers") / "redaction-layers-000005"
        shutil.copyfile(case_pdf / "redaction-layers-000005.pdf", run_dir / "output.pdf")
        self.assertEqual(run("collect", str(run_dir))[0], 0)
        code, _, err = run("score", str(run_dir), "--dpi", "100", "--no-ocr")
        self.assertEqual(code, 0, err)
        self.assertTrue(list((run_dir / "report" / "overlay").glob("*-overlay.png")))

    def test_a_sweep_that_finds_one_run_still_fills_reports(self):
        run("generate", "redaction-layers", "-s", "5")
        self._collected_run(DemoWeb, "redaction-layers-000005")
        code, _, err = run("score", "--dpi", "100", "--no-ocr")
        self.assertEqual(code, 0, err)
        tool = self.ws.reports_dir("demo:web")
        self.assertTrue((tool / "report.html").exists())
        self.assertTrue(list((tool / "redaction-layers" / "overlay").glob("*.png")))
        self.assertFalse(self.ws.reports_dir().exists(), "one tool: no summary")

    def test_no_overlay_draws_none(self):
        run("generate", "redaction-layers", "-s", "5")
        run_dir = self._collected_run(DemoWeb, "redaction-layers-000005")
        code, _, err = run("score", str(run_dir), "--dpi", "100", "--no-ocr",
                           "--no-overlay")
        self.assertEqual(code, 0, err)
        self.assertTrue((run_dir / "report" / "report.html").exists())
        self.assertFalse((run_dir / "report" / "overlay").exists())

    def test_two_unexplained_pdfs_are_refused_not_guessed(self):
        run("generate", "redaction-layers", "-s", "5")
        registry.register(DemoWeb, replace=True)
        run("submit", "demo:web", "redaction-layers-000005")
        run_dir = next(self.ws.runs_dir("demo:web").iterdir())
        for name in ("redacted.pdf", "redacted (1).pdf"):
            (run_dir / name).write_bytes(b"%PDF-1.7\nx\n%%EOF\n")
        code, _, err = run("collect", str(run_dir))
        self.assertEqual(code, 1)
        self.assertIn("2 PDFs", err)
        self.assertFalse((run_dir / "manifest.json").exists())

        (run_dir / "redacted (1).pdf").unlink()
        self.assertEqual(run("collect", str(run_dir))[0], 0)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["output_name"], "redacted.pdf")

    def test_nothing_to_score_is_a_clean_error(self):
        code, _, err = run("score")
        self.assertEqual(code, 1)
        self.assertIn("nothing to score", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
