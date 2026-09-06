"""End-to-end HTTPS coverage with a self-signed local fixture.

The scenario server is HTTP-only by design, so this module mints a fresh
self-signed certificate with openssl, serves a fixed response over TLS on
loopback, and drives the real executor through it. Skipped entirely when
openssl is unavailable. No external network is used.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

from http_load_tester.domain.errors import ErrorCategory
from http_load_tester.domain.models import HttpRequest, Origin, Outcome, TestPlan, TimeoutConfig
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.pool.connection_pool import ConnectionPool

HAS_OPENSSL = shutil.which("openssl") is not None

RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok"
)


def _read_request(connection: socket.socket) -> bool:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            return False
        data.extend(chunk)
        if len(data) > 64 * 1024:
            return False
    return True


class LocalTlsServer:
    """Serve one fixed response over TLS until closed."""

    def __init__(self, cert_path: str, key_path: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self._context = context
        self._listener = listener
        self._closed = threading.Event()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    def start(self) -> None:
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw: socket.socket) -> None:
        try:
            with self._context.wrap_socket(raw, server_side=True) as connection:
                connection.settimeout(5)
                while _read_request(connection):
                    try:
                        connection.sendall(RESPONSE)
                    except OSError:
                        return
        except (OSError, ssl.SSLError):
            pass

    def close(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass


def _mint_certificate(home: str) -> tuple[str, str]:
    cert_path = home + "/server.pem"
    key_path = home + "/server.key"
    config_path = home + "/openssl.cnf"
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write(
            "[req]\n"
            "distinguished_name = dn\n"
            "x509_extensions = ext\n"
            "prompt = no\n"
            "[dn]\n"
            "CN = localhost\n"
            "[ext]\n"
            "subjectAltName = DNS:localhost,IP:127.0.0.1\n"
        )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "1",
            "-nodes",
            "-config",
            config_path,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return cert_path, key_path


@unittest.skipUnless(HAS_OPENSSL, "openssl is required for the TLS fixture")
class TlsEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._home.cleanup)
        cls.cert_path, cls.key_path = _mint_certificate(cls._home.name)
        cls.server = LocalTlsServer(cls.cert_path, cls.key_path)
        cls.server.start()
        cls.addClassCleanup(cls.server.close)

    def _run(
        self, *, tls_verify: bool, request_count: int = 2
    ) -> tuple:
        origin = Origin("https", "127.0.0.1", self.server.port, tls_verify=tls_verify)
        timeouts = TimeoutConfig(
            connect_seconds=5, request_seconds=10, pool_acquire_seconds=10
        )
        plan = TestPlan(
            origin=origin,
            request=HttpRequest("GET", "/"),
            request_count=request_count,
            workers=1,
            max_connections=1,
            timeouts=timeouts,
        )
        pool = ConnectionPool(origin, 1, timeouts)
        return WorkExecutor(plan, pool).run()

    def test_verify_enabled_run_succeeds_and_reuses(self) -> None:
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = self.cert_path
        try:
            samples = self._run(tls_verify=True)
        finally:
            if previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous
        self.assertEqual(len(samples), 2)
        for sample in samples:
            self.assertEqual(sample.outcome, Outcome.SUCCESS)
            self.assertEqual(sample.status_code, 200)
        self.assertFalse(samples[0].connection_reused)
        self.assertTrue(samples[1].connection_reused)

    def test_untrusted_certificate_is_rejected(self) -> None:
        previous = os.environ.pop("SSL_CERT_FILE", None)
        try:
            (sample,) = self._run(tls_verify=True, request_count=1)
        finally:
            if previous is not None:
                os.environ["SSL_CERT_FILE"] = previous
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.TLS_FAILURE)
        self.assertIsNone(sample.status_code)

    def test_insecure_run_succeeds_without_trust(self) -> None:
        previous = os.environ.pop("SSL_CERT_FILE", None)
        try:
            samples = self._run(tls_verify=False)
        finally:
            if previous is not None:
                os.environ["SSL_CERT_FILE"] = previous
        self.assertEqual(len(samples), 2)
        for sample in samples:
            self.assertEqual(sample.outcome, Outcome.SUCCESS)
            self.assertEqual(sample.status_code, 200)


if __name__ == "__main__":
    unittest.main()
