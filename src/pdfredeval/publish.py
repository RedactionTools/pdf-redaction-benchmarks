"""Publish scored runs, and cases, to redaction-tools.com.

The last step of `generate -> submit -> collect -> score -> report -> publish`. Stdlib
only (urllib, a hand-rolled multipart body), so publishing works from the same bare
environment `generate` does.

What leaves the machine is exactly what the site needs to show a result and nothing it
must not see: per run, `manifest.json`, `score/report.json`, the delivered PDF and the
overlay if one was drawn. Never `ground_truth.json` (its seed regenerates the case),
never `probes.csv`, screenshots or `entities.json`. Cases, with their ground truth, go
only through `publish-cases`, which the site accepts from staff keys alone.

The site rescores every published PDF with its own copy of this scorer and badges the
result `verified` or `disputed`, so a report is a claim the site checks, not one it
takes on trust.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BenchmarkError
from .manifest import RunManifest

DEFAULT_SITE = "https://backend.redaction-tools.com"
SUBMISSIONS_PAGE = "https://redaction-tools.com/benchmarks/submissions"
API_KEY_ENV = "PDFREDEVAL_API_KEY"
SITE_ENV = "PDFREDEVAL_SITE"
TIMEOUT_SECONDS = 120


class PublishError(BenchmarkError):
    """The site refused, or could not be reached. The message is the site's own."""


@dataclass(frozen=True, slots=True)
class PublishableRun:
    run_dir: Path
    manifest: RunManifest
    report: Path
    pdf: Path
    overlay: Path | None

    @property
    def group(self) -> tuple[str, str]:
        """Runs of one tool against one revision go up as one submission."""
        return self.manifest.tool_id, str(self.manifest.dataset_revision)


def load_run(run_dir: Path) -> PublishableRun:
    """Everything `publish` would send for `run_dir`, or why it cannot be sent."""
    from .score import REPORT_NAME, SCORE_DIR
    from .workspace import output_pdf_path

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise BenchmarkError(f"{run_dir} has no manifest.json. Collect it first: "
                             f"pdfredeval collect {run_dir}")
    manifest = RunManifest.load(manifest_path)
    if missing := manifest.missing_fields():
        raise BenchmarkError(f"{run_dir}: the manifest is missing {', '.join(missing)}; "
                             "the site publishes a result only with all of them")
    report = run_dir / SCORE_DIR / REPORT_NAME
    if not report.exists():
        raise BenchmarkError(f"{run_dir} is not scored. Score it first: "
                             f"pdfredeval score {run_dir}")
    pdf = output_pdf_path(run_dir, manifest.case_id, manifest.tool_id, manifest.output_name)
    if not pdf.exists():
        raise BenchmarkError(f"{run_dir}: the delivered PDF {pdf.name} is missing")
    return PublishableRun(run_dir, manifest, report, pdf, find_overlay(run_dir, manifest))


def find_overlay(run_dir: Path, manifest: RunManifest) -> Path | None:
    """The overlay `score` drew for this run, beside it or in the tree's reports/.

    Optional: the site draws its own when the run arrives without one.
    """
    name = f"{manifest.run_id}-overlay.png"
    candidates = [run_dir / "report" / "overlay" / name]
    for parent in run_dir.parents:
        if (parent / "reports").is_dir():
            candidates.extend((parent / "reports").glob(f"*/*/overlay/{name}"))
            break
    return next((path for path in candidates if path.exists()), None)


class SiteClient:
    """The few routes of the site's benchmarks API that the CLI uses."""

    def __init__(self, site: str, api_key: str) -> None:
        self.base = site.rstrip("/") + "/api/v1/benchmarks"
        self.api_key = api_key

    def open_submission(self, suite: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/suites/{suite}/submissions", json_body=body)

    def publish_run(self, submission_id: str, run: PublishableRun) -> dict[str, Any]:
        files = {
            "manifest": ("manifest.json", json.dumps(run.manifest.to_dict()).encode()),
            "report": ("report.json", run.report.read_bytes()),
            "pdf": (run.pdf.name, run.pdf.read_bytes()),
        }
        if run.overlay:
            files["overlay"] = (run.overlay.name, run.overlay.read_bytes())
        return self._post(f"/submissions/{submission_id}/runs", files=files)

    def finalize(self, submission_id: str) -> dict[str, Any]:
        return self._post(f"/submissions/{submission_id}/finalize")

    def publish_case(self, suite: str, case_dir: Path, visibility: str) -> dict[str, Any]:
        truth_path = case_dir / "ground_truth.json"
        truth = json.loads(truth_path.read_text())
        pdf = case_dir / (truth.get("pdf") or f"{case_dir.name}.pdf")
        return self._post(
            f"/suites/{suite}/cases",
            fields={"visibility": visibility},
            files={
                "pdf": (pdf.name, pdf.read_bytes()),
                "ground_truth": ("ground_truth.json", truth_path.read_bytes()),
            },
        )

    def _post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        fields: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if files is not None:
            data, content_type = _multipart(fields or {}, files)
            headers["Content-Type"] = content_type
        else:
            data = json.dumps(json_body or {}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - the site URL is the user's own setting
            self.base + path, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                body: dict[str, Any] = json.loads(response.read() or b"{}")
                return body
        except urllib.error.HTTPError as exc:
            raise PublishError(_refusal(exc)) from exc
        except urllib.error.URLError as exc:
            raise PublishError(f"could not reach {self.base}: {exc.reason}") from exc


def _refusal(exc: urllib.error.HTTPError) -> str:
    """The site's own words for a refusal: its `detail`, or the field errors."""
    try:
        detail = json.loads(exc.read()).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        detail = "; ".join(str(item.get("msg", item)) for item in detail)
    if exc.code == 401:
        return f"the site did not accept the API key in ${API_KEY_ENV} (401)"
    return f"the site refused ({exc.code}): {detail or exc.reason}"


def _multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes]]
) -> tuple[bytes, str]:
    boundary = f"pdfredeval-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode() + b"\r\n"
        )
    for name, (filename, content) in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + content + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def client_from_env(site: str | None) -> SiteClient:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise BenchmarkError(
            f"set ${API_KEY_ENV} to an API key from https://redaction-tools.com/account. "
            "It is read from the environment only, so it never lands in shell history."
        )
    return SiteClient(site or os.environ.get(SITE_ENV) or DEFAULT_SITE, api_key)


def publish_runs(
    client: SiteClient,
    runs: Sequence[PublishableRun],
    *,
    suite: str = "pdf",
    notes: str = "",
) -> list[tuple[str, int]]:
    """One submission per (tool, revision), every run in it, then send it for review.

    Returns `(submission id, runs sent)` per submission.
    """
    groups: dict[tuple[str, str], list[PublishableRun]] = defaultdict(list)
    for run in runs:
        groups[run.group].append(run)

    sent = []
    for (tool_id, revision), members in groups.items():
        slug, _, surface = tool_id.rpartition(":")
        first = members[0].manifest
        submission = client.open_submission(suite, {
            "tool": slug,
            "surface": surface,
            "origin": "cli",
            "revision": revision,
            "tool_version": first.tool_version or "",
            "tier": first.tier or "",
            "notes": notes,
        })
        for run in members:
            client.publish_run(submission["id"], run)
        client.finalize(submission["id"])
        sent.append((submission["id"], len(members)))
    return sent


def case_dirs(root: Path) -> Iterable[Path]:
    """Every case under `root`: a directory holding a ground_truth.json."""
    return sorted(path.parent for path in root.rglob("ground_truth.json"))
