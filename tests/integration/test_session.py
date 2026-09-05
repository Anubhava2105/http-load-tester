import socket
import threading
import unittest

from http_load_tester.domain.errors import ConnectionClosedEarly
from http_load_tester.domain.models import HttpRequest, Origin, TimeoutConfig
from http_load_tester.http.session import Http1Session
from http_load_tester.transport.tcp import TcpTransport


class LocalHttpServer:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses
        self.requests = 0
        self.accepts = 0
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]

    def start(self) -> None:
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            with self._listener:
                connection, _ = self._listener.accept()
                self.accepts += 1
                with connection:
                    for response in self._responses:
                        self._read_request(connection)
                        self.requests += 1
                        connection.sendall(response)
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()

    @staticmethod
    def _read_request(connection: socket.socket) -> None:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                raise RuntimeError("client closed before request headers")
            data.extend(chunk)


class SessionIntegrationTests(unittest.TestCase):
    def test_keep_alive_uses_one_connection_for_two_requests(self) -> None:
        server = LocalHttpServer(
            [
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
                b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nbye",
            ]
        )
        server.start()
        session = Http1Session(
            Origin("http", "127.0.0.1", server.port),
            TcpTransport.connect(
                Origin("http", "127.0.0.1", server.port),
                TimeoutConfig(connect_seconds=1),
            ),
        )

        try:
            first = session.execute(HttpRequest("GET", "/one"))
            second = session.execute(HttpRequest("GET", "/two"))
        finally:
            session.close()
        server.done.wait(1)

        self.assertIsNone(server.error)
        self.assertEqual(server.requests, 2)
        self.assertEqual(server.accepts, 1)
        self.assertEqual(first.body_bytes, 2)
        self.assertEqual(second.body_bytes, 3)
        self.assertTrue(session.last_timing is not None)

    def test_connection_close_response_prevents_second_exchange(self) -> None:
        server = LocalHttpServer(
            [b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok"]
        )
        server.start()
        origin = Origin("http", "127.0.0.1", server.port)
        session = Http1Session(
            origin,
            TcpTransport.connect(
                origin,
                TimeoutConfig(connect_seconds=1),
            ),
        )

        try:
            response = session.execute(HttpRequest("GET", "/"))
            self.assertFalse(response.connection_reusable)
            with self.assertRaises(ConnectionClosedEarly):
                session.execute(HttpRequest("GET", "/again"))
        finally:
            session.close()
        server.done.wait(1)

        self.assertIsNone(server.error)
        self.assertEqual(server.requests, 1)


if __name__ == "__main__":
    unittest.main()
