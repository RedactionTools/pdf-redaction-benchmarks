"""`pdfredeval login` / `logout`, and where `publish` gets its key.

Against a stub of the site on localhost, as in test_publish.py: the device-code
exchange is real HTTP, and the browser is the only thing replaced.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from pdfredeval.cli import main

KEY = "Zz99Yy88.s3cret"


class Site:
    """A stub site: scripted poll answers, and every request it saw."""

    def __init__(self, polls: list[dict[str, Any]]) -> None:
        self.polls = list(polls)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - quiet
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                headers = {k.lower(): v for k, v in self.headers.items()}
                is_json = "json" in headers.get("content-type", "")
                body = json.loads(raw) if raw and is_json else {}
                site.requests.append((self.path, headers, body))
                if self.path == "/api/v1/auth/cli-logins":
                    self._reply(201, {
                        "device_code": "dev-secret", "user_code": "BCDF-GHJK",
                        "verification_url": "https://example.test/cli/login",
                        "verification_url_complete": "https://example.test/cli/login?code=BCDF-GHJK",
                        "expires_in": 600, "interval": 0,
                    })
                elif self.path == "/api/v1/auth/cli-logins/token":
                    self._reply(200, site.polls.pop(0))
                elif self.path == "/api/v1/auth/api-keys/current/revoke":
                    self.send_response(204)
                    self.end_headers()
                else:
                    self._reply(201, {"id": "sub-1"})

            def _reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]


APPROVED = {"status": "approved", "key": KEY, "user": {"name": "Mykola", "email": "m@x.test"}}


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Path(tempfile.mkdtemp())
        self.env = {"PDFREDEVAL_CONFIG_DIR": str(self.config), "PDFREDEVAL_API_KEY": ""}

    def run_cli(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **(env or {})}), \
                mock.patch("webbrowser.open") as browser, \
                redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        self.browser = browser
        return code, out.getvalue(), err.getvalue()

    def stored(self) -> dict[str, Any]:
        return json.loads((self.config / "credentials.json").read_text())

    def test_login_opens_the_browser_and_saves_the_key_it_is_given(self):
        site = Site([{"status": "pending"}, APPROVED])

        code, out, err = self.run_cli("login", "--site", site.url)

        self.assertEqual(code, 0, err)
        self.browser.assert_called_once_with("https://example.test/cli/login?code=BCDF-GHJK")
        self.assertIn("BCDF-GHJK", out)
        self.assertIn("Logged in as Mykola", out)
        self.assertEqual(self.stored()["sites"][site.url]["api_key"], KEY)
        _, _, started = site.requests[0]
        self.assertTrue(started["client_name"])

    def test_the_saved_key_is_readable_by_its_owner_only(self):
        site = Site([APPROVED])

        self.run_cli("login", "--site", site.url)

        mode = stat.S_IMODE((self.config / "credentials.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_browser_only_prints_the_link(self):
        site = Site([APPROVED])

        _, out, _ = self.run_cli("login", "--site", site.url, "--no-browser")

        self.browser.assert_not_called()
        self.assertIn("https://example.test/cli/login?code=BCDF-GHJK", out)

    def test_a_denied_login_saves_nothing(self):
        site = Site([{"status": "denied"}])

        code, _, err = self.run_cli("login", "--site", site.url)

        self.assertEqual(code, 1)
        self.assertIn("denied", err)
        self.assertFalse((self.config / "credentials.json").exists())

    def test_publish_uses_the_saved_key(self):
        site = Site([APPROVED])
        self.run_cli("login", "--site", site.url)

        from pdfredeval.publish import client_for

        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(client_for(site.url).api_key, KEY)

    def test_the_environment_key_wins_over_the_saved_one(self):
        site = Site([APPROVED])
        self.run_cli("login", "--site", site.url)

        from pdfredeval.publish import client_for

        with mock.patch.dict(os.environ, {**self.env, "PDFREDEVAL_API_KEY": "Env1.key"}):
            self.assertEqual(client_for(site.url).api_key, "Env1.key")

    def test_no_key_at_all_points_at_both_ways_to_get_one(self):
        from pdfredeval.errors import BenchmarkError
        from pdfredeval.publish import client_for

        with mock.patch.dict(os.environ, self.env), \
                self.assertRaises(BenchmarkError) as raised:
            client_for("http://127.0.0.1:9")
        self.assertIn("pdfredeval login", str(raised.exception))
        self.assertIn("PDFREDEVAL_API_KEY", str(raised.exception))

    def test_logout_revokes_the_key_and_forgets_it(self):
        site = Site([APPROVED])
        self.run_cli("login", "--site", site.url)

        code, out, _ = self.run_cli("logout", "--site", site.url)

        self.assertEqual(code, 0)
        path, headers, _ = site.requests[-1]
        self.assertEqual(path, "/api/v1/auth/api-keys/current/revoke")
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertNotIn(site.url, self.stored()["sites"])
        self.assertIn("Logged out", out)
