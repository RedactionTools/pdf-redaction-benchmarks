"""Which certificates `publish` and `login` verify the site with.

The python.org installers for macOS ship an OpenSSL that trusts nothing until
"Install Certificates.command" is run - so every HTTPS call fails with
CERTIFICATE_VERIFY_FAILED on a perfectly valid certificate. These pin the fallback.
"""

from __future__ import annotations

import ssl
import sys
import types
import unittest
from unittest import mock

from pdfredeval import publish
from pdfredeval.errors import BenchmarkError


def _empty_default_context() -> ssl.SSLContext:
    """A client context with no CA certificates loaded - the broken python.org state."""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class TlsContextTests(unittest.TestCase):
    def test_uses_pythons_own_store_when_it_has_certificates(self):
        # Not the real default: on a machine with the bug, that is exactly the empty one.
        stocked = mock.Mock(spec=ssl.SSLContext)
        stocked.cert_store_stats.return_value = {"x509": 140, "crl": 0, "x509_ca": 140}

        with mock.patch("ssl.create_default_context", return_value=stocked):
            self.assertIs(publish.ssl_context(), stocked)

    def test_falls_back_to_the_system_store_when_python_trusts_nothing(self):
        system = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        fake = types.SimpleNamespace(SSLContext=mock.Mock(return_value=system))

        with mock.patch("ssl.create_default_context", _empty_default_context), \
                mock.patch.dict(sys.modules, {"truststore": fake}):
            context = publish.ssl_context()

        self.assertIs(context, system)
        fake.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)

    def test_then_to_certifi(self):
        fake_certifi = types.SimpleNamespace(where=lambda: "/tmp/certifi.pem")
        loaded: list[str] = []

        def default_context(cafile: str | None = None) -> ssl.SSLContext:
            loaded.append(cafile or "")
            return _empty_default_context()

        with mock.patch("ssl.create_default_context", default_context), \
                mock.patch.dict(sys.modules, {"truststore": None, "certifi": fake_certifi}):
            publish.ssl_context()

        self.assertEqual(loaded, ["", "/tmp/certifi.pem"])

    def test_with_nothing_to_trust_it_says_how_to_fix_it(self):
        with mock.patch("ssl.create_default_context", _empty_default_context), \
                mock.patch.dict(sys.modules, {"truststore": None, "certifi": None}), \
                self.assertRaises(BenchmarkError) as raised:
            publish.ssl_context()

        message = str(raised.exception)
        self.assertIn("Install Certificates", message)
        self.assertIn("uv sync", message)
