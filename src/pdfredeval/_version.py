"""The package version, resolved once from installed metadata.

`pyproject.toml` is the only place a version is written. A copy in the source would drift
from it silently - and this version is stamped into every generated page's banner, its
`/Producer`, and its ground truth, so a stale copy is a case claiming to come from a
generator that did not make it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

DISTRIBUTION = "pdfredeval"

#: Marker used when the package is not installed (a source tree run without `uv sync`).
#: Deliberately not a plausible version number: a case stamped with this should be
#: recognisable as one whose provenance could not be established.
UNKNOWN_VERSION = "0+unknown"


def resolve() -> str:
    try:
        return _distribution_version(DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover - needs an uninstalled tree
        return UNKNOWN_VERSION


__version__ = resolve()
