"""Run provenance. Fields per docs/workflow.md.

Tools change silently and carry no version, so a score measures *a tool on a date*. A
result missing these fields is not publishable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SecretLeakError
from .types import Transport

# Keys whose values must never reach a manifest. Credentials come from the environment;
# the manifest records *that* an account and tier were used, not the secret.
_SECRET_KEY = re.compile(
    r"(api[-_]?key|secret|token|password|passwd|credential|authorization|auth[-_]?header"
    r"|bearer|session[-_]?id|cookie|private[-_]?key)",
    re.I,
)


def assert_no_secrets(data: Mapping[str, Any], *, path: str = "") -> None:
    """Raise if any key in a nested mapping looks like a credential.

    Settings are recorded verbatim, which is exactly how a key ends up in a published
    manifest. This is the guard that makes "verbatim" safe.
    """
    for key, value in data.items():
        here = f"{path}.{key}" if path else str(key)
        if _SECRET_KEY.search(str(key)):
            raise SecretLeakError(
                f"refusing to record {here!r} in a manifest: it looks like a credential. "
                "Pass credentials via the environment and record only the tier."
            )
        if isinstance(value, Mapping):
            assert_no_secrets(value, path=here)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class RunManifest:
    run_id: str
    case_id: str
    tool_id: str
    transport: Transport
    observed_at: str = field(default_factory=utcnow)
    attempt: int = 1
    dataset_revision: str | None = None
    tool_version: str | None = None
    url: str | None = None
    tier: str | None = None
    operator: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    vendor_settings: dict[str, Any] = field(default_factory=dict)
    input_sha256: str | None = None
    output_sha256: str | None = None
    #: The delivered file inside the run directory, under the name it arrived with.
    output_name: str | None = None
    notes: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.transport, str):
            self.transport = Transport(self.transport)
        assert_no_secrets(self.profile)
        assert_no_secrets(self.vendor_settings)

    @property
    def complete(self) -> bool:
        """True when this manifest carries everything a published score requires."""
        return not self.missing_fields()

    def missing_fields(self) -> list[str]:
        required = (
            "run_id", "case_id", "tool_id", "observed_at",
            "dataset_revision", "input_sha256", "output_sha256",
        )
        return [f for f in required if not getattr(self, f)]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["transport"] = self.transport.value
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RunManifest:
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def load(cls, path: Path | str) -> RunManifest:
        return cls.from_dict(json.loads(Path(path).read_text()))
