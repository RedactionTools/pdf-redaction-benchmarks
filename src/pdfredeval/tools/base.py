"""The `Tool` contract: submit -> poll -> fetch.

Two-phase because vendor APIs are commonly job-based, and because the manual path is the
same shape with a slower transport - submit writes a task, poll asks whether the operator
has dropped the file yet, fetch reads it. Treating manual as the degenerate async case is
what keeps one code path through align, probe and score. See docs/adapters.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..capabilities import Capabilities
from ..errors import (
    InvalidToolIdError,
    ManualInterventionRequired,
    NotReadyError,
    SubmissionFailed,
    SubmissionRejected,
    SubmissionTimeout,
)
from ..manifest import RunManifest, utcnow
from ..types import Case, Status, Surface, Transport
from ..workspace import LEGACY_OUTPUT_PDF, output_pdf_path, tool_slug

TOOL_ID_RE = re.compile(r"^(?P<vendor>[a-z0-9][a-z0-9._-]*):(?P<surface>web|api|desktop)$")

#: What a pre-versioned run called its output; still accepted, no longer written.
OUTPUT_NAME = LEGACY_OUTPUT_PDF
HANDLE_NAME = "handle.json"
MANIFEST_NAME = "manifest.json"


def parse_tool_id(tool_id: str) -> tuple[str, Surface]:
    """Split `vendor:surface`, or raise."""
    match = TOOL_ID_RE.match(tool_id or "")
    if not match:
        raise InvalidToolIdError(
            f"{tool_id!r} is not a valid tool id. Expected `vendor:surface` where surface "
            f"is one of web, api, desktop - e.g. 'acme:web'."
        )
    return match["vendor"], Surface(match["surface"])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class Handle:
    """A submission in flight.

    Persisted, because the manual path spans processes and days: an operator submits on
    Monday and the output is scored on Wednesday by a different invocation.
    """

    run_id: str
    tool_id: str
    case_id: str
    run_dir: Path
    transport: Transport
    attempt: int = 1
    created_at: str = field(default_factory=utcnow)
    vendor_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        if isinstance(self.transport, str):
            self.transport = Transport(self.transport)

    @property
    def output_path(self) -> Path:
        """The delivered PDF under whatever name it arrived with; see `output_pdf_path`."""
        return output_pdf_path(self.run_dir, self.case_id, self.tool_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_dir"] = str(self.run_dir)
        d["transport"] = self.transport.value
        return d

    def save(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / HANDLE_NAME
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def load(cls, run_dir: Path | str) -> Handle:
        run_dir = Path(run_dir)
        return cls(**json.loads((run_dir / HANDLE_NAME).read_text()))


@dataclass(slots=True)
class RunResult:
    handle: Handle
    manifest: RunManifest
    output_path: Path
    output_sha256: str


class Tool(ABC):
    """Base class for every adapter, manual or API.

    Subclasses set `tool_id`, `transport` and `capabilities`, then implement the three
    transport methods. Everything else - run directories, handles, manifests, polling
    with backoff - is inherited, so an adapter is small and hard to get wrong.
    """

    tool_id: ClassVar[str] = ""
    transport: ClassVar[Transport] = Transport.API
    url: ClassVar[str | None] = None

    #: Poll pacing for `wait()`; overridden by manual adapters, which wait on a person.
    poll_interval: ClassVar[float] = 2.0
    poll_backoff: ClassVar[float] = 1.5
    poll_interval_max: ClassVar[float] = 30.0

    def __init__(
        self,
        *,
        runs_dir: Path | str = "runs",
        tier: str | None = None,
        operator: str | None = None,
    ) -> None:
        self.vendor, self.surface = parse_tool_id(self.tool_id)
        self.runs_dir = Path(runs_dir)
        self.tier = tier
        self.operator = operator

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.tool_id!r}>"

    # --- what the adapter declares ------------------------------------------------

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Declared scope. Drives `unsupported` in scoring."""

    @property
    def version(self) -> str | None:
        """Vendor version if discoverable. None is honest; guessing is not."""
        return None

    # --- what the adapter implements ----------------------------------------------

    def normalize_settings(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        """Map our profile onto vendor parameters.

        Both are recorded in the manifest, so a result is reproducible by someone not
        using this framework. Default: pass the profile through untouched.
        """
        return dict(profile)

    @abstractmethod
    def _submit(self, case: Case, settings: Mapping[str, Any], handle: Handle) -> None:
        """Start the job. Set `handle.vendor_ref` if the vendor returns one.

        Raise `SubmissionRejected` if the tool refuses the input.
        """

    @abstractmethod
    def poll(self, handle: Handle) -> Status:
        """PENDING, READY, FAILED or REJECTED. Must not block."""

    @abstractmethod
    def fetch(self, handle: Handle) -> bytes:
        """The output bytes, exactly as the vendor returned them.

        No re-encoding, no unwrapping, no tidying: the artefacts this benchmark hunts for
        live precisely in the parts a helpful client library would strip.
        """

    # --- orchestration, inherited -------------------------------------------------

    def submit(
        self,
        case: Case,
        profile: Mapping[str, Any] | None = None,
        *,
        attempt: int = 1,
        run_id: str | None = None,
    ) -> Handle:
        """Create the run directory, check the input against declared limits, submit."""
        profile = dict(profile or {})
        pdf = case.pdf_bytes
        reason = self.capabilities.accepts(pages=self._page_count(pdf), size_bytes=len(pdf))
        if reason:
            raise SubmissionRejected(f"{self.tool_id}: {reason}")

        run_id = run_id or self._new_run_id(case, attempt)
        handle = Handle(
            run_id=run_id,
            tool_id=self.tool_id,
            case_id=case.case_id,
            run_dir=self.runs_dir / run_id,
            transport=self.transport,
            attempt=attempt,
        )
        handle.run_dir.mkdir(parents=True, exist_ok=True)
        settings = self.normalize_settings(profile)
        handle.extra["profile"] = profile
        handle.extra["vendor_settings"] = settings
        handle.extra["input_sha256"] = sha256(pdf)
        handle.extra["dataset_revision"] = case.dataset_revision
        # Run identity is fixed at submit time and must survive to collection: on the
        # manual path those are different processes, days apart, and the tool object
        # rebuilt at collect time knows none of it.
        handle.extra["operator"] = self.operator
        handle.extra["tier"] = self.tier
        handle.extra["url"] = self.url
        handle.extra["tool_version"] = self.version
        self._submit(case, settings, handle)
        handle.save()
        return handle

    def wait(self, handle: Handle, *, timeout: float = 300.0) -> Status:
        """Poll until terminal or `timeout` seconds elapse, backing off between polls."""
        deadline = time.monotonic() + timeout
        interval = self.poll_interval
        while True:
            status = self.poll(handle)
            if status.terminal:
                return status
            if time.monotonic() >= deadline:
                return Status.PENDING
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * self.poll_backoff, self.poll_interval_max)

    def collect(self, handle: Handle) -> RunResult:
        """Fetch the output, write it byte-identical, and emit the manifest."""
        status = self.poll(handle)
        if status is Status.REJECTED:
            raise SubmissionRejected(f"{self.tool_id} rejected run {handle.run_id}")
        if status is Status.FAILED:
            raise SubmissionFailed(f"{self.tool_id} failed run {handle.run_id}")
        if status is not Status.READY:
            raise NotReadyError(
                f"run {handle.run_id} is {status.value}; nothing to collect yet"
            )

        data = self.fetch(handle)
        # Written exactly as delivered. Re-saving through any PDF library can strip the
        # earlier revisions, metadata and annotations the leak checks look for.
        handle.output_path.write_bytes(data)
        manifest = self.manifest(handle, output_sha256=sha256(data))
        manifest.save(handle.run_dir / MANIFEST_NAME)
        return RunResult(
            handle=handle,
            manifest=manifest,
            output_path=handle.output_path,
            output_sha256=manifest.output_sha256 or "",
        )

    def run(
        self,
        case: Case,
        profile: Mapping[str, Any] | None = None,
        *,
        attempt: int = 1,
        timeout: float = 300.0,
    ) -> RunResult:
        """submit -> wait -> collect, for adapters that can complete unattended."""
        if self.transport is Transport.MANUAL:
            raise ManualInterventionRequired(
                f"{self.tool_id} needs a person. Call submit() to create the task, have "
                f"the operator drop the output in the run directory, then collect()."
            )
        handle = self.submit(case, profile, attempt=attempt)
        status = self.wait(handle, timeout=timeout)
        if status is Status.PENDING:
            raise SubmissionTimeout(
                f"{self.tool_id} run {handle.run_id} still pending after {timeout}s. "
                f"The handle is saved in {handle.run_dir}; collect() it later."
            )
        # collect() owns the status -> exception mapping, so REJECTED and FAILED stay
        # distinguishable on both paths.
        return self.collect(handle)

    def manifest(self, handle: Handle, *, output_sha256: str | None = None) -> RunManifest:
        # Prefer what the handle recorded at submit time; fall back to this instance,
        # so a field left unset then can still be supplied at collection.
        extra = handle.extra
        return RunManifest(
            run_id=handle.run_id,
            case_id=handle.case_id,
            tool_id=self.tool_id,
            transport=self.transport,
            attempt=handle.attempt,
            dataset_revision=extra.get("dataset_revision"),
            tool_version=extra.get("tool_version") or self.version,
            url=extra.get("url") or self.url,
            tier=extra.get("tier") or self.tier,
            operator=extra.get("operator") or self.operator,
            profile=dict(handle.extra.get("profile") or {}),
            vendor_settings=dict(handle.extra.get("vendor_settings") or {}),
            input_sha256=handle.extra.get("input_sha256"),
            output_sha256=output_sha256,
            output_name=handle.output_path.name,
            capabilities=self.capabilities.to_dict(),
        )

    # --- helpers ------------------------------------------------------------------

    def _new_run_id(self, case: Case, attempt: int) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        slug = tool_slug(self.tool_id)
        return f"{stamp}-{slug}-{case.case_id}-a{attempt}-{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _page_count(pdf: bytes) -> int:
        """Cheap page count without a PDF dependency.

        Counts `/Type /Page` occurrences, which is adequate for the single-page synthetic
        cases this framework generates. A real parser belongs behind the aligner.
        """
        return max(1, len(re.findall(rb"/Type\s*/Page[^s]", pdf)))
