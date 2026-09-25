"""`pdfredeval publish` and `publish-cases`, against a stub of the site's API.

The stub is a real HTTP server on localhost, so the multipart encoding and the headers
are exercised exactly as the site receives them - not a mocked opener.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from pdfredeval.cli import main

RUN_ID = "20260925T081446-acme-web-pii-detection-1-a1-21dbff"


class Recorded:
    """What the stub server saw: (method, path, headers, parsed body) per request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.fail_with: tuple[int, dict] | None = None


def _parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    parts = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = (part.get_filename(), part.get_payload(decode=True))
    return parts


def _serve(recorded: Recorded) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - quiet
            pass

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            body: Any = (
                _parse_multipart(ctype, raw) if ctype.startswith("multipart/")
                else json.loads(raw or b"{}")
            )
            recorded.requests.append(
                ("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            if recorded.fail_with:
                self._reply(*recorded.fail_with)
            elif self.path.endswith("/submissions"):
                self._reply(201, {"id": "sub-1", "status": "draft"})
            elif self.path.endswith("/finalize"):
                self._reply(200, {"id": "sub-1", "status": "scoring"})
            else:
                self._reply(201, {"run_id": RUN_ID})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server



def run(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=False), \
            redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _scored_run(root: Path, *, scored: bool = True, overlay: bool = True) -> Path:
    run_dir = root / "runs" / "acme-web" / RUN_ID
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": RUN_ID, "case_id": "pii-detection-1", "tool_id": "acme:web",
        "transport": "manual", "observed_at": "2026-09-25T08:14:47+00:00", "attempt": 1,
        "dataset_revision": "v0.1.1", "input_sha256": "a" * 64, "output_sha256": "b" * 64,
        "output_name": "redacted.pdf", "tier": "Pro", "tool_version": "4.2",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "redacted.pdf").write_bytes(b"%PDF-1.7 redacted")
    (run_dir / "ground_truth.json").write_text('{"seed": 1}')  # must never be sent
    if scored:
        (run_dir / "score").mkdir()
        (run_dir / "score" / "report.json").write_text(
            json.dumps({"run_id": RUN_ID, "case_id": "pii-detection-1", "summary": {}})
        )
        (run_dir / "score" / "probes.csv").write_text("probe_id\n")  # must never be sent
    if overlay:
        (run_dir / "report" / "overlay").mkdir(parents=True)
        (run_dir / "report" / "overlay" / f"{RUN_ID}-overlay.png").write_bytes(b"\x89PNG")
    return run_dir


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = Recorded()
        self.server = _serve(self.recorded)
        self.site = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"PDFREDEVAL_API_KEY": "Ab12Cd34.secret", "PDFREDEVAL_SITE": self.site}

    def tearDown(self) -> None:
        self.server.shutdown()

    def test_publishes_a_scored_run_as_one_submission(self):
        run_dir = _scored_run(self.tmp)

        code, out, err = run("publish", str(run_dir), "--notes", "first pass", env=self.env)

        self.assertEqual(code, 0, err)
        paths = [path for _, path, _, _ in self.recorded.requests]
        self.assertEqual(paths, [
            "/api/v1/benchmarks/suites/pdf/submissions",
            "/api/v1/benchmarks/submissions/sub-1/runs",
            "/api/v1/benchmarks/submissions/sub-1/finalize",
        ])
        _, _, headers, opened = self.recorded.requests[0]
        self.assertEqual(headers["x-api-key"], "Ab12Cd34.secret")
        self.assertEqual(opened, {
            "tool": "acme", "surface": "web", "origin": "cli", "revision": "v0.1.1",
            "tool_version": "4.2", "tier": "Pro", "notes": "first pass",
        })
        self.assertIn("1 run", out)

    def test_sends_the_run_files_and_nothing_else(self):
        run_dir = _scored_run(self.tmp)

        run("publish", str(run_dir), env=self.env)

        _, _, _, parts = self.recorded.requests[1]
        self.assertEqual(set(parts), {"manifest", "report", "pdf", "overlay"})
        self.assertEqual(parts["pdf"], ("redacted.pdf", b"%PDF-1.7 redacted"))
        self.assertEqual(json.loads(parts["manifest"][1])["run_id"], RUN_ID)
        sent = b"".join(data for _, data in parts.values())
        self.assertNotIn(b'"seed"', sent)
        self.assertNotIn(b"probe_id", sent)

    def test_a_run_without_an_overlay_is_still_published(self):
        run("publish", str(_scored_run(self.tmp, overlay=False)), env=self.env)

        _, _, _, parts = self.recorded.requests[1]
        self.assertNotIn("overlay", parts)

    def test_an_unscored_run_is_refused_before_anything_is_sent(self):
        code, _, err = run("publish", str(_scored_run(self.tmp, scored=False)), env=self.env)

        self.assertEqual(code, 1)
        self.assertIn("pdfredeval score", err)
        self.assertEqual(self.recorded.requests, [])

    def test_no_api_key_is_a_clear_error(self):
        env = {"PDFREDEVAL_SITE": self.site, "PDFREDEVAL_API_KEY": ""}

        code, _, err = run("publish", str(_scored_run(self.tmp)), env=env)

        self.assertEqual(code, 1)
        self.assertIn("PDFREDEVAL_API_KEY", err)

    def test_a_dry_run_sends_nothing(self):
        code, out, _ = run("publish", str(_scored_run(self.tmp)), "--dry-run", env=self.env)

        self.assertEqual(code, 0)
        self.assertIn("acme:web", out)
        self.assertEqual(self.recorded.requests, [])

    def test_the_sites_refusal_is_shown_verbatim(self):
        self.recorded.fail_with = (422, {"detail": "The catalog has no published tool 'acme'."})

        code, _, err = run("publish", str(_scored_run(self.tmp)), env=self.env)

        self.assertEqual(code, 1)
        self.assertIn("no published tool 'acme'", err)


class PublishCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = Recorded()
        self.server = _serve(self.recorded)
        self.env = {
            "PDFREDEVAL_API_KEY": "k.s",
            "PDFREDEVAL_SITE": f"http://127.0.0.1:{self.server.server_address[1]}",
        }
        self.cases = Path(tempfile.mkdtemp())
        for case_id in ("pii-detection-1", "pii-detection-2"):
            case = self.cases / "pii-detection" / case_id
            case.mkdir(parents=True)
            (case / f"{case_id}.pdf").write_bytes(b"%PDF-1.7 " + case_id.encode())
            (case / "ground_truth.json").write_text(
                json.dumps({"case_id": case_id, "pdf": f"{case_id}.pdf", "seed": 1})
            )

    def tearDown(self) -> None:
        self.server.shutdown()

    def test_sends_each_case_with_its_ground_truth_and_visibility(self):
        code, out, err = run(
            "publish-cases", "--cases-dir", str(self.cases),
            "--holdout", "pii-detection-2", env=self.env,
        )

        self.assertEqual(code, 0, err)
        sent = {
            json.loads(body["ground_truth"][1])["case_id"]: body
            for _, _, _, body in self.recorded.requests
        }
        self.assertEqual(set(sent), {"pii-detection-1", "pii-detection-2"})
        self.assertEqual(sent["pii-detection-1"]["visibility"], (None, b"public"))
        self.assertEqual(sent["pii-detection-2"]["visibility"], (None, b"holdout"))
        self.assertEqual(
            sent["pii-detection-1"]["pdf"],
            ("pii-detection-1.pdf", b"%PDF-1.7 pii-detection-1"),
        )
        self.assertIn("2 cases", out)
