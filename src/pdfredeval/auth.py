"""`pdfredeval login` and `logout`: an API key for this machine, without copying one.

A device-code sign-in (RFC 8628 in spirit), so it works from any terminal, SSH
included: the site hands out a short code, the browser opens on redaction-tools.com
where the user signs in and approves that code, and the next poll returns a fresh API
key. The key never appears in a URL or the browser's history, and no local port listens.

Keys are saved per site in `credentials.json` under `$PDFREDEVAL_CONFIG_DIR` (default
`$XDG_CONFIG_HOME/pdfredeval`, i.e. `~/.config/pdfredeval`), created readable by the
owner only. `$PDFREDEVAL_API_KEY`, when set, still wins - for CI, where there is no
browser to sign in with.
"""

from __future__ import annotations

import json
import os
import socket
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import BenchmarkError

CREDENTIALS_FILE = "credentials.json"


def config_dir() -> Path:
    if explicit := os.environ.get("PDFREDEVAL_CONFIG_DIR"):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pdfredeval"


def _credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILE


def _load() -> dict[str, Any]:
    path = _credentials_path()
    if not path.exists():
        return {"sites": {}}
    data: dict[str, Any] = json.loads(path.read_text())
    data.setdefault("sites", {})
    return data


def _save(data: dict[str, Any]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created 0600 before a byte is written, rather than chmod-ed after: there is no
    # moment at which the key sits in a world-readable file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT


def stored_key(site: str) -> str | None:
    entry = _load()["sites"].get(site)
    return entry.get("api_key") if entry else None


def login(
    site: str,
    *,
    open_browser: bool = True,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Sign this machine in to `site`. Returns the user the key belongs to."""
    from .publish import post

    started = post(f"{site}/api/v1/auth/cli-logins",
                   json_body={"client_name": socket.gethostname() or "pdfredeval"})
    link = started["verification_url_complete"]
    say(f"Your code: {started['user_code']}")
    say(f"Approve it at {link}")
    if open_browser and webbrowser.open(link):
        say("Opened your browser. Waiting for approval...")
    else:
        say("Open that link in a browser. Waiting for approval...")

    deadline = time.monotonic() + float(started["expires_in"])
    interval = max(float(started.get("interval") or 0), 0.0)
    while time.monotonic() < deadline:
        answer = post(f"{site}/api/v1/auth/cli-logins/token",
                      json_body={"device_code": started["device_code"]})
        status = answer.get("status")
        if status == "approved":
            data = _load()
            data["sites"][site] = {"api_key": answer["key"], "user": answer.get("user") or {}}
            _save(data)
            user: dict[str, Any] = answer.get("user") or {}
            return user
        if status != "pending":
            raise BenchmarkError(f"sign-in {status}. Run `pdfredeval login` to try again.")
        sleep(interval)
    raise BenchmarkError(
        "sign-in expired before it was approved. Run `pdfredeval login` again."
    )


def logout(site: str) -> bool:
    """Revoke this machine's key on `site` and forget it. False if there was none."""
    from .publish import PublishError, post

    data = _load()
    entry = data["sites"].pop(site, None)
    if entry is None:
        return False
    try:
        post(f"{site}/api/v1/auth/api-keys/current/revoke", api_key=entry["api_key"])
    except PublishError:
        # Already revoked on /account, or the site is unreachable: forgetting it here
        # is still what was asked for. The key stays listed on /account to revoke there.
        pass
    _save(data)
    return True
