"""Exception hierarchy. Every failure mode the adapter contract can produce."""

from __future__ import annotations


class BenchmarkError(Exception):
    """Base for everything this package raises."""


class RegistryError(BenchmarkError):
    """Registry lookup or registration problem."""


class UnknownToolError(RegistryError):
    """No adapter registered under that id."""


class DuplicateToolError(RegistryError):
    """Two adapters claim the same id; the second was rejected."""


class InvalidToolIdError(RegistryError):
    """Tool id is not `vendor:surface`."""


class AdapterError(BenchmarkError):
    """Base for adapter-side failures."""


class MissingCredentialError(AdapterError):
    """A credential env var the adapter needs is unset."""


class SubmissionRejected(AdapterError):
    """The tool refused the input: too many pages, wrong format, quota."""


class SubmissionFailed(AdapterError):
    """The tool accepted the input and then failed to produce output."""


class SubmissionTimeout(AdapterError):
    """The job was still pending when the caller's deadline elapsed.

    Not a failure: the handle is on disk and the run can be collected later.
    """


class NotReadyError(AdapterError):
    """fetch() was called before poll() reported READY."""


class ManualInterventionRequired(AdapterError):
    """run() was called on a manual adapter, which cannot complete unattended."""


class SecretLeakError(BenchmarkError):
    """A credential-shaped value was about to be written to a manifest."""
