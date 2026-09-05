import threading
import unittest

from http_load_tester.domain.clock import MonotonicClock
from http_load_tester.domain.errors import ConnectionClosedEarly
from http_load_tester.domain.models import HttpRequest, Origin, SafetyLimits
from http_load_tester.http.session import Http1Session


class ScriptedTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._responses = list(responses)
        self.closed = False

    def send_all(self, data: bytes, deadline_ns: int | None = None) -> None:
        self.sent.append(data)

    def recv_some(self, max_bytes: int, deadline_ns: int | None = None) -> bytes:
        if not self._responses:
            return b""
        response = self._responses.pop(0)
        return response[:max_bytes]

    def close(self) -> None:
        self.closed = True


class SessionTests(unittest.TestCase):
    def test_execute_encodes_writes_parses_and_records_timing(self) -> None:
        transport = ScriptedTransport(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
        )
        session = Http1Session(
            Origin("http", "example.test", 80),
            transport,
            SafetyLimits(),
            clock=MonotonicClock(),
        )

        response = session.execute(HttpRequest("GET", "/health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body_bytes, 2)
        self.assertTrue(session.reusable)
        self.assertEqual(len(transport.sent), 1)
        self.assertIn(b"GET /health HTTP/1.1\r\n", transport.sent[0])
        self.assertIsNotNone(session.last_timing)
        self.assertLessEqual(
            session.last_timing.write_start_ns,
            session.last_timing.completion_ns,
        )
        self.assertIsNotNone(session.last_timing.first_byte_ns)

    def test_connection_close_makes_session_non_reusable(self) -> None:
        transport = ScriptedTransport(
            [b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"]
        )
        session = Http1Session(Origin("http", "example.test", 80), transport)

        response = session.execute(HttpRequest("GET", "/"))

        self.assertFalse(response.connection_reusable)
        self.assertFalse(session.reusable)
        with self.assertRaises(ConnectionClosedEarly):
            session.execute(HttpRequest("GET", "/again"))

    def test_request_connection_close_makes_session_non_reusable(self) -> None:
        transport = ScriptedTransport(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"]
        )
        session = Http1Session(Origin("http", "example.test", 80), transport)

        response = session.execute(
            HttpRequest("GET", "/", headers=(("Connection", "close"),))
        )

        self.assertFalse(response.connection_reusable)
        self.assertFalse(session.reusable)

    def test_failure_closes_reuse_and_serializes_execution(self) -> None:
        transport = ScriptedTransport(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"]
        )
        session = Http1Session(Origin("http", "example.test", 80), transport)
        entered = threading.Event()
        finished = threading.Event()

        def run() -> None:
            entered.set()
            session.execute(HttpRequest("GET", "/"))
            finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        thread.join(timeout=1)

        self.assertTrue(finished.is_set())
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(session.reusable)


if __name__ == "__main__":
    unittest.main()
