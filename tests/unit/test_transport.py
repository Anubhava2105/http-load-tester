import socket
import ssl
import threading
import time
import unittest

from http_load_tester.domain.errors import ReadTimeout
from http_load_tester.domain.models import Origin, TimeoutConfig
from http_load_tester.transport.tcp import TcpTransport
from http_load_tester.transport.tls import create_ssl_context


class TcpTransportTests(unittest.TestCase):
    def test_tcp_round_trip_uses_bounded_send_and_receive(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        server_done = threading.Event()
        server_received: list[bytes] = []

        def serve() -> None:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    server_received.append(connection.recv(5))
                    connection.sendall(b"world")
                    server_done.set()

        server = threading.Thread(target=serve)
        server.start()
        transport = TcpTransport.connect(
            Origin("http", "127.0.0.1", port),
            TimeoutConfig(connect_seconds=1),
        )
        try:
            deadline_ns = time.monotonic_ns() + 1_000_000_000
            transport.send_all(b"hello", deadline_ns)
            self.assertEqual(transport.recv_some(5, deadline_ns), b"world")
        finally:
            transport.close()
        server.join(timeout=1)

        self.assertEqual(server_received, [b"hello"])
        self.assertTrue(server_done.is_set())

    def test_read_deadline_maps_to_read_timeout(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = threading.Event()
        release = threading.Event()

        def serve() -> None:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    accepted.set()
                    release.wait(1)

        server = threading.Thread(target=serve)
        server.start()
        transport = TcpTransport.connect(
            Origin("http", "127.0.0.1", port),
            TimeoutConfig(connect_seconds=1),
        )
        try:
            self.assertTrue(accepted.wait(1))
            with self.assertRaises(ReadTimeout):
                transport.recv_some(1, time.monotonic_ns() + 50_000_000)
        finally:
            release.set()
            transport.close()
        server.join(timeout=1)

    def test_close_is_idempotent(self) -> None:
        left, right = socket.socketpair()
        right.close()
        transport = TcpTransport(left)
        transport.close()
        transport.close()
        self.assertTrue(transport._closed)


class TlsContextTests(unittest.TestCase):
    def test_tls_context_verifies_by_default_and_can_be_explicitly_disabled(
        self,
    ) -> None:
        verified = create_ssl_context(Origin("https", "example.test", 443))
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(verified.check_hostname)

        insecure = create_ssl_context(
            Origin("https", "example.test", 443, tls_verify=False)
        )
        self.assertEqual(insecure.verify_mode, ssl.CERT_NONE)
        self.assertFalse(insecure.check_hostname)


if __name__ == "__main__":
    unittest.main()
