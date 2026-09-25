"""The API transport: credentials, throttling, retries.

Deliberately free of any HTTP client dependency - an adapter brings its own. What this
class provides is the conduct the docs require: credentials from the environment, throttle
by default, and retries that back off instead of hammering a vendor.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

from ..errors import MissingCredentialError
from ..types import Transport
from .base import Tool

R = TypeVar("R")


class Throttle:
    """Minimum-interval limiter. Monotonic, so a clock change cannot unblock it."""

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


class ApiTool(Tool):
    """Base for programmatic adapters.

    Subclasses implement `_submit`, `poll` and `fetch` using whatever client they like,
    routing calls through `self.call()` to inherit throttling and retry.
    """

    transport: ClassVar[Transport] = Transport.API

    #: Environment variable holding the credential. Never read from a file in the repo.
    credential_env: ClassVar[str | None] = None
    #: Requests per second ceiling. Conservative by default: a banned account yields no data.
    max_requests_per_second: ClassVar[float] = 2.0
    max_retries: ClassVar[int] = 3
    retry_backoff: ClassVar[float] = 2.0
    #: Exceptions worth retrying. Subclasses narrow this to their client's transient errors.
    retry_on: ClassVar[tuple[type[BaseException], ...]] = (TimeoutError, ConnectionError)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        interval = 1.0 / self.max_requests_per_second if self.max_requests_per_second else 0.0
        self.throttle = Throttle(interval)

    # --- credentials ---------------------------------------------------------------

    def credential(self, *, required: bool = True) -> str | None:
        """Read the credential from the environment.

        Never returned in a manifest, never logged. The manifest records the tier, not
        the secret - see `manifest.assert_no_secrets`.
        """
        if not self.credential_env:
            return None
        value = os.environ.get(self.credential_env)
        if not value and required:
            raise MissingCredentialError(
                f"{self.tool_id} needs {self.credential_env} in the environment. "
                f"Export it for this shell; do not put it in a file in the repo."
            )
        return value

    @property
    def authenticated(self) -> bool:
        return not self.credential_env or bool(os.environ.get(self.credential_env))

    # --- conduct -------------------------------------------------------------------

    def call(self, fn: Callable[[], R]) -> R:
        """Run one vendor request, throttled, with backoff on transient failure."""
        attempt = 0
        while True:
            self.throttle.wait()
            try:
                return fn()
            except self.retry_on:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                # Jitter, so parallel runs against one vendor do not synchronise.
                delay = self.retry_backoff ** attempt
                time.sleep(delay * (0.5 + random.random() / 2))
