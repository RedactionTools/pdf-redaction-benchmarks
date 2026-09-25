"""Contract tests for the adapter layer.

Runs on stdlib unittest so `python -m unittest` works with no dependencies installed;
pytest collects it too.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pdfredeval import (
    ApiTool,
    Capabilities,
    Case,
    Conditions,
    DuplicateToolError,
    InvalidToolIdError,
    ManualInterventionRequired,
    ManualTool,
    MissingCredentialError,
    NotReadyError,
    Probe,
    ProbeKind,
    Severity,
    Stage,
    Status,
    SubmissionRejected,
    Transport,
    UnknownToolError,
)
from pdfredeval.errors import SecretLeakError
from pdfredeval.manifest import assert_no_secrets
from pdfredeval.tools import registry

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page /Parent 2 0 R>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)

FULL_CAPS = Capabilities(
    stages=frozenset({Stage.DETECTION, Stage.REDACTION}),
    categories=frozenset({"PERSON", "EMAIL"}),
    channels=frozenset({"text_layer", "metadata"}),
    orientations=frozenset({"horizontal", "rot90"}),
    polarities=frozenset({"normal", "inverse"}),
    provenances=frozenset({"vector", "print-clean", "hand-block"}),
)


def make_case(tmp: Path) -> Case:
    pdf = tmp / "case.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    return Case(
        case_id="c1",
        pdf_path=pdf,
        dataset_revision="rev-abc",
        probes=(
            Probe(
                id="p1", kind=ProbeKind.TEXT_SPAN, must_redact=True, category="PERSON",
                value="Whitfield Diffie", units=("Whitfield", "Diffie"),
                severity=Severity.CRITICAL,
            ),
            Probe(
                id="p2", kind=ProbeKind.TEXT_SPAN, must_redact=False, category="ORG",
                value="Market Supplies Ltd",
            ),
        ),
    )


class FakeVendor:
    """Stand-in for a job-based vendor API."""

    def __init__(self, *, pending_polls: int = 1, reject: bool = False) -> None:
        self.jobs: dict[str, int] = {}
        self.pending_polls = pending_polls
        self.reject = reject
        self.calls = 0

    def submit(self, data: bytes) -> str:
        self.calls += 1
        job = f"job-{len(self.jobs) + 1}"
        self.jobs[job] = 0
        return job

    def status(self, job: str) -> str:
        self.calls += 1
        if self.reject:
            return "rejected"
        self.jobs[job] += 1
        return "ready" if self.jobs[job] > self.pending_polls else "pending"

    def download(self, job: str) -> bytes:
        self.calls += 1
        return MINIMAL_PDF.replace(b"/Type/Page ", b"/Type/Page /Redacted true ")


class DemoApi(ApiTool):
    tool_id = "demo:api"
    url = "https://api.demo.example/v1/redact"
    credential_env = "DEMO_API_KEY"
    max_requests_per_second = 1000.0  # keep the suite fast
    poll_interval = 0.001

    def __init__(self, vendor: FakeVendor, **kw):
        super().__init__(**kw)
        self.vendor = vendor

    @property
    def capabilities(self) -> Capabilities:
        return FULL_CAPS

    @property
    def version(self) -> str:
        return "2026.09"

    def normalize_settings(self, profile):
        mapping = {"PERSON": "names", "EMAIL": "emails"}
        return {
            "targets": [mapping[c] for c in profile.get("redact", []) if c in mapping],
            "strict": profile.get("mode") == "strict",
        }

    def _submit(self, case, settings, handle):
        self.credential()
        handle.vendor_ref = self.call(lambda: self.vendor.submit(case.pdf_bytes))

    def poll(self, handle):
        state = self.call(lambda: self.vendor.status(handle.vendor_ref))
        return {
            "pending": Status.PENDING, "ready": Status.READY,
            "failed": Status.FAILED, "rejected": Status.REJECTED,
        }[state]

    def fetch(self, handle):
        return self.call(lambda: self.vendor.download(handle.vendor_ref))


class DemoWeb(ManualTool):
    tool_id = "demo:web"
    url = "https://demo.example/redact"
    instructions = "Choose 'Full redaction', not 'Preview'."

    @property
    def capabilities(self) -> Capabilities:
        return FULL_CAPS


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_register_and_lookup(self):
        registry.register(DemoApi, replace=True)
        registry.register(DemoWeb, replace=True)
        self.assertEqual(registry.get("demo:api"), DemoApi)
        self.assertIn("demo:web", registry.available())

    def test_web_and_api_are_separate_entries(self):
        """A vendor's surfaces must not collapse into one entry."""
        registry.register(DemoApi, replace=True)
        registry.register(DemoWeb, replace=True)
        self.assertEqual(registry.surfaces_of("demo"), ["demo:api", "demo:web"])
        self.assertEqual(registry.available(transport=Transport.MANUAL), ["demo:web"])
        self.assertEqual(registry.available(transport=Transport.API), ["demo:api"])

    def test_duplicate_is_rejected(self):
        registry.register(DemoApi, replace=True)

        with self.assertRaises(DuplicateToolError):
            class Clash(ApiTool):
                tool_id = "demo:api"

                @property
                def capabilities(self):
                    return FULL_CAPS

                def _submit(self, case, settings, handle): ...
                def poll(self, handle): return Status.READY
                def fetch(self, handle): return b""

            registry.register(Clash)

    def test_invalid_id_rejected_at_registration(self):
        with self.assertRaises(InvalidToolIdError):
            class Bad(ManualTool):
                tool_id = "no-surface"

                @property
                def capabilities(self):
                    return FULL_CAPS

            registry.register(Bad)

    def test_unknown_tool_lists_alternatives(self):
        registry.register(DemoApi, replace=True)
        with self.assertRaises(UnknownToolError) as ctx:
            registry.get("nope:api")
        self.assertIn("demo:api", str(ctx.exception))


class CapabilityTests(unittest.TestCase):
    def test_out_of_scope_probes_give_a_reason(self):
        caps = FULL_CAPS
        cursive = Probe(
            id="x", kind=ProbeKind.TEXT_SPAN, must_redact=True, category="PERSON",
            value="Jane Roe", conditions=Conditions(provenance="hand-cursive"),
        )
        face = Probe(id="f", kind=ProbeKind.FACE, must_redact=True)
        self.assertIn("hand-cursive", caps.unsupported_reason(cursive))
        self.assertIn("face", caps.unsupported_reason(face))
        self.assertIsNone(caps.unsupported_reason(
            Probe(id="ok", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                  category="EMAIL", value="a@b.com")
        ))

    def test_coverage_statement_names_the_gaps(self):
        gaps = " | ".join(FULL_CAPS.coverage_statement())
        # hand-block is declared; cursive and mixed are not, and must be named
        self.assertIn("hand-cursive", gaps)
        self.assertIn("hand-mixed", gaps)
        self.assertNotIn("hand-block", gaps)
        self.assertIn("vertical", gaps)
        self.assertNotIn("inverse", gaps)  # inverse IS declared

    def test_page_limit_rejects_oversized_input(self):
        caps = Capabilities(max_pages=1)
        self.assertIsNone(caps.accepts(pages=1, size_bytes=100))
        self.assertIn("exceeds", caps.accepts(pages=5, size_bytes=100))

    def test_roundtrip(self):
        self.assertEqual(Capabilities.from_dict(FULL_CAPS.to_dict()), FULL_CAPS)


class ApiPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.case = make_case(self.root)
        os.environ["DEMO_API_KEY"] = "test-key"

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DEMO_API_KEY", None)

    def _tool(self, **kw) -> DemoApi:
        return DemoApi(FakeVendor(**kw), runs_dir=self.root / "runs", tier="free")

    def test_run_produces_output_and_manifest(self):
        tool = self._tool()
        result = tool.run(self.case, {"redact": ["PERSON", "EMAIL"], "mode": "strict"})
        self.assertTrue(result.output_path.exists())
        self.assertTrue(result.manifest.complete, result.manifest.missing_fields())
        m = result.manifest
        self.assertEqual(m.tool_id, "demo:api")
        self.assertEqual(m.transport, Transport.API)
        self.assertEqual(m.tool_version, "2026.09")
        self.assertEqual(m.dataset_revision, "rev-abc")
        self.assertEqual(m.tier, "free")
        # both the normalized profile and the raw vendor payload are recorded
        self.assertEqual(m.profile["mode"], "strict")
        self.assertEqual(m.vendor_settings, {"targets": ["names", "emails"], "strict": True})

    def test_output_is_written_byte_identical(self):
        tool = self._tool()
        result = tool.run(self.case, {})
        expected = FakeVendor().download("job-1")
        self.assertEqual(result.output_path.read_bytes(), expected)

    def test_manifest_lands_on_disk_and_reloads(self):
        from pdfredeval.manifest import RunManifest

        result = self._tool().run(self.case, {})
        path = result.handle.run_dir / "manifest.json"
        self.assertEqual(RunManifest.load(path).run_id, result.handle.run_id)

    def test_credential_comes_from_env(self):
        del os.environ["DEMO_API_KEY"]
        with self.assertRaises(MissingCredentialError):
            self._tool().run(self.case, {})

    def test_credential_never_reaches_manifest(self):
        result = self._tool().run(self.case, {})
        blob = json.dumps(result.manifest.to_dict())
        self.assertNotIn("test-key", blob)

    def test_rejected_submission_surfaces(self):
        tool = self._tool(reject=True)
        with self.assertRaises(SubmissionRejected):
            tool.run(self.case, {})

    def test_oversized_input_rejected_before_any_vendor_call(self):
        vendor = FakeVendor()
        tool = DemoApi(vendor, runs_dir=self.root / "runs")
        big = Case(case_id="big", pdf_path=self.case.pdf_path, dataset_revision="r")
        with unittest.mock.patch.object(
            DemoApi, "capabilities",
            property(lambda self: Capabilities(max_pages=0)),
        ):
            with self.assertRaises(SubmissionRejected):
                tool.submit(big, {})
        self.assertEqual(vendor.calls, 0)

    def test_repeat_attempts_are_separate_runs(self):
        tool = self._tool()
        a = tool.run(self.case, {}, attempt=1)
        b = tool.run(self.case, {}, attempt=2)
        self.assertNotEqual(a.handle.run_id, b.handle.run_id)
        self.assertEqual((a.manifest.attempt, b.manifest.attempt), (1, 2))


class ManualPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.case = make_case(self.root)
        self.tool = DemoWeb(runs_dir=self.root / "runs", operator="mm", tier="free")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_refuses_and_says_what_to_do(self):
        with self.assertRaises(ManualInterventionRequired) as ctx:
            self.tool.run(self.case, {})
        self.assertIn("submit()", str(ctx.exception))

    def test_submit_writes_a_task_for_the_operator(self):
        handle = self.tool.submit(self.case, {"redact": ["PERSON"]})
        task = (handle.run_dir / "TASK.md").read_text()
        self.assertIn("demo:web", task)
        self.assertIn("exactly as downloaded", task)
        self.assertIn("Choose 'Full redaction'", task)
        self.assertEqual(self.tool.poll(handle), Status.PENDING)

    def test_collect_before_delivery_raises(self):
        handle = self.tool.submit(self.case, {})
        with self.assertRaises(NotReadyError):
            self.tool.collect(handle)

    def test_full_manual_cycle(self):
        handle = self.tool.submit(self.case, {})
        delivered = b"%PDF-1.7\n redacted by hand\n%%EOF\n"
        handle.output_path.write_bytes(delivered)
        self.assertEqual(self.tool.poll(handle), Status.READY)
        result = self.tool.collect(handle)
        # bytes untouched by collection
        self.assertEqual(result.output_path.read_bytes(), delivered)
        self.assertTrue(result.manifest.complete, result.manifest.missing_fields())
        self.assertEqual(result.manifest.transport, Transport.MANUAL)
        self.assertEqual(result.manifest.operator, "mm")

    def test_handle_survives_a_process_boundary(self):
        """The operator submits today and the output is scored days later."""
        from pdfredeval.tools.base import Handle

        handle = self.tool.submit(self.case, {"mode": "strict"})
        reloaded = Handle.load(handle.run_dir)
        self.assertEqual(reloaded.run_id, handle.run_id)
        self.assertEqual(reloaded.extra["profile"], {"mode": "strict"})
        reloaded.output_path.write_bytes(MINIMAL_PDF)
        self.assertTrue(self.tool.collect(reloaded).manifest.complete)


class SecretGuardTests(unittest.TestCase):
    def test_credential_shaped_keys_are_blocked(self):
        for bad in ({"api_key": "x"}, {"outer": {"Authorization": "Bearer y"}},
                    {"X-Auth-Token": "z"}, {"password": "p"}):
            with self.assertRaises(SecretLeakError):
                assert_no_secrets(bad)

    def test_benign_settings_pass(self):
        assert_no_secrets({"mode": "strict", "tier": "free", "redact": ["PERSON"]})


class GroundTruthTests(unittest.TestCase):
    def test_value_is_always_a_disclosure_unit(self):
        p = Probe(id="p", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                  value="Whitfield Diffie", units=("Diffie",))
        self.assertEqual(p.units[0], "Whitfield Diffie")

    def test_lexical_conflicts_are_detectable(self):
        """docs/datasets/core.md: a token must belong to one value on the page."""
        tmp = tempfile.TemporaryDirectory()
        pdf = Path(tmp.name) / "c.pdf"
        pdf.write_bytes(MINIMAL_PDF)
        clean = make_case(Path(tmp.name))
        self.assertEqual(clean.lexical_conflicts(), {})
        dirty = Case(case_id="d", pdf_path=pdf, probes=(
            Probe(id="t", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                  value="John Smith", units=("Smith",)),
            Probe(id="d1", kind=ProbeKind.TEXT_SPAN, must_redact=False,
                  value="Smith & Co", units=("Smith",)),
        ))
        self.assertIn("smith", dirty.lexical_conflicts())

        # The sanctioned repeat: the same value under another rendering condition is
        # one subject planted twice, not two values quarrelling over a token.
        repeated = Case(case_id="r", pdf_path=pdf, probes=(
            Probe(id="t1", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                  value="John Smith", units=("John Smith", "John", "Smith")),
            Probe(id="t2", kind=ProbeKind.TEXT_SPAN, must_redact=True,
                  value="John Smith", units=("John Smith", "John", "Smith")),
        ))
        self.assertEqual(repeated.lexical_conflicts(), {})
        tmp.cleanup()

    def test_severity_weights_double(self):
        self.assertEqual(
            [Severity.LOW.weight, Severity.MEDIUM.weight,
             Severity.HIGH.weight, Severity.CRITICAL.weight],
            [1, 2, 4, 8],
        )

    def test_condition_levels_validated(self):
        with self.assertRaises(ValueError):
            Conditions(orientation="sideways")
        self.assertTrue(Conditions().is_control)
        self.assertTrue(Conditions(provenance="hand-cursive").is_handwritten)


if __name__ == "__main__":
    unittest.main(verbosity=2)
