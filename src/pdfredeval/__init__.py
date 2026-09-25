"""Evaluation framework for third-party PDF redaction tools.

Design and metric definitions live in `docs/`; this package implements them. The pipeline
is generate -> submit -> collect -> align -> probe -> score, and each stage has a module:
`generate/`, `tools/`, `align`, `probe/`, `score/`.

Scoring needs a PDF parser and a rasteriser, so `align`, `probe` and `score` are imported
lazily below: `pdfredeval generate` and the adapter layer must keep working in an
environment that never installed the score extra.
"""

from typing import TYPE_CHECKING, Any

from ._version import __version__ as _package_version
from .capabilities import Capabilities
from .errors import (
    AdapterError,
    BenchmarkError,
    DuplicateToolError,
    InvalidToolIdError,
    ManualInterventionRequired,
    MissingCredentialError,
    NotReadyError,
    RegistryError,
    SecretLeakError,
    SubmissionFailed,
    SubmissionRejected,
    SubmissionTimeout,
    UnknownToolError,
)
from .manifest import RunManifest
from .textmatch import Disclosure, disclosure, normalize
from .thresholds import DEFAULTS, DISCLOSURE_LENGTH, Thresholds
from .tools import (
    ApiTool,
    Handle,
    ManualTool,
    RunResult,
    Tool,
    available,
    create,
    get,
    register,
)
from .types import (
    BBox,
    Case,
    Channel,
    Conditions,
    Difficulty,
    Fiducial,
    Mode,
    Probe,
    ProbeKind,
    Severity,
    Stage,
    Status,
    Surface,
    Transport,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .align import Alignment, Frame
    from .probe import Observations
    from .score import ProbeRow, ScoreResult, score_run

#: Types served on demand, so importing this package does not import pandas or pypdf.
#:
#: The stage *functions* are deliberately not re-exported: `pdfredeval.score` is the
#: scoring package, and an attribute of the same name would shadow it the moment anything
#: imported the module. Call them where they live - `from pdfredeval.score import score`,
#: `from pdfredeval.probe import probe`, `from pdfredeval.align import align`.
_LAZY = {
    "Alignment": "align",
    "Frame": "align",
    "Observations": "probe",
    "ProbeRow": "score",
    "ScoreResult": "score",
    "score_run": "score",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f".{module}", __name__), name)


# Single source of truth: pyproject.toml, read back through installed metadata.
__version__ = _package_version

__all__ = [
    "AdapterError",
    "Alignment",
    "ApiTool",
    "BBox",
    "BenchmarkError",
    "Capabilities",
    "Case",
    "Channel",
    "Conditions",
    "DEFAULTS",
    "DISCLOSURE_LENGTH",
    "Difficulty",
    "Disclosure",
    "DuplicateToolError",
    "Fiducial",
    "Frame",
    "Handle",
    "InvalidToolIdError",
    "ManualInterventionRequired",
    "ManualTool",
    "MissingCredentialError",
    "Mode",
    "NotReadyError",
    "Observations",
    "Probe",
    "ProbeKind",
    "ProbeRow",
    "RegistryError",
    "RunManifest",
    "RunResult",
    "ScoreResult",
    "SecretLeakError",
    "Severity",
    "Stage",
    "Status",
    "SubmissionFailed",
    "SubmissionRejected",
    "SubmissionTimeout",
    "Surface",
    "Thresholds",
    "Tool",
    "Transport",
    "UnknownToolError",
    "__version__",
    "available",
    "create",
    "disclosure",
    "get",
    "normalize",
    "register",
    "score_run",
]
