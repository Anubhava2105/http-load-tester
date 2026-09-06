"""Deterministic scenario and integration coverage (Stage 16).

Exercised entirely against the local HTTP-only scenario server; no
external network is used.
"""

from __future__ import annotations

import io
import socket
import struct
import threading
import unittest

from http_load_tester.application.config import load_plan
from http_load_tester.application.runner import run_plan
from http_load_tester.domain.errors import ConnectionReset, ErrorCategory
from http_load_tester.domain.models import (
    HttpRequest,
    Origin,
    Outcome,
    SafetyLimits,
    TestPlan,
    TimeoutConfig,
)
from http_load_tester.http.response_parser import ResponseParser
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.pool.connection_pool import ConnectionPool
from test_server import Scenario, ScenarioConfig, ScenarioServer


def _origin_for(server: ScenarioServer) -> Origin:
    host, port = server.address
    assert host in ("127.0.0.1", "::1", "localhost")
    return Origin("http", host, port)


def _run(
    server: ScenarioServer,
    *,
    request_count: int | None = 2,
    duration_seconds: float | None = None,
    timeouts: TimeoutConfig | None = None,
    limits: SafetyLimits | None = None,
    workers: int = 1,
    max_connections: int = 1,
) -> tuple[TestPlan, tuple]:
    origin = _origin_for(server)
    selected_timeouts = timeouts or TimeoutConfig(
        connect_seconds=2,
        request_seconds=5,
        pool_acquire_seconds=5,
    )
    selected_limits = limits or SafetyLimits()
    plan = TestPlan(
        origin=origin,
        request=HttpRequest("GET", "/"),
        request_count=request_count,
        duration_seconds=duration_seconds,
        workers=workers,
        max_connections=max_connections,
        timeouts=selected_timeouts,
        limits=selected_limits,
    )
    pool = ConnectionPool(origin, max_connections, selected_timeouts, selected_limits)
    samples = WorkExecutor(plan, pool).run()
    return plan, samples


def _serve_one_raw(payload: bytes) -> tuple[str, int, threading.Thread]:
    """Serve one raw payload on loopback, then close the listener."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def _serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                seen = bytearray()
                while b"\r\n\r\n" not in seen:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    seen.extend(chunk)
                try:
                    connection.sendall(payload)
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return host, port, thread


def _run_against_raw(payload: bytes) -> tuple:
    listener_host, listener_port, thread = _serve_one_raw(payload)
    origin = Origin("http", listener_host, listener_port)
    timeouts = TimeoutConfig(
        connect_seconds=2,
        request_seconds=5,
        pool_acquire_seconds=5,
    )
    plan = TestPlan(
        origin=origin,
        request=HttpRequest("GET", "/"),
        request_count=1,
        workers=1,
        max_connections=1,
        timeouts=timeouts,
    )
    pool = ConnectionPool(origin, 1, timeouts)
    samples = WorkExecutor(plan, pool).run()
    thread.join(timeout=2)
    return samples


class ChunkReader:
    """Feed a fixed payload one fragment at a time."""

    def __init__(self, payload: bytes, chunk_size: int = 1) -> None:
        self._payload = payload
        self._chunk_size = chunk_size

    def recv_some(self, max_bytes: int, deadline_ns: int | None = None) -> bytes:
        del deadline_ns
        if not self._payload:
            return b""
        amount = min(max_bytes, self._chunk_size, len(self._payload))
        chunk = self._payload[:amount]
        self._payload = self._payload[amount:]
        return chunk


class DelayedScenarioTests(unittest.TestCase):
    def test_delayed_headers_triggers_read_timeout(self) -> None:
        config = ScenarioConfig(scenario=Scenario.DELAYED_HEADERS, delay_seconds=0.4)
        with ScenarioServer(config) as server:
            timeouts = TimeoutConfig(
                connect_seconds=2,
                request_seconds=0.1,
                pool_acquire_seconds=5,
            )
            _, samples = _run(server, request_count=1, timeouts=timeouts)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.outcome, Outcome.TIMEOUT)
        self.assertEqual(sample.error_category, ErrorCategory.READ_TIMEOUT)
        self.assertIsNone(sample.status_code)

    def test_delayed_body_chunks_trigger_read_timeout(self) -> None:
        config = ScenarioConfig(
            scenario=Scenario.DELAYED_BODY,
            body=b"hello-world",
            delay_seconds=0.4,
            chunk_size=2,
        )
        with ScenarioServer(config) as server:
            timeouts = TimeoutConfig(
                connect_seconds=2,
                request_seconds=0.1,
                pool_acquire_seconds=5,
            )
            _, samples = _run(server, request_count=1, timeouts=timeouts)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.outcome, Outcome.TIMEOUT)
        self.assertEqual(sample.error_category, ErrorCategory.READ_TIMEOUT)

    def test_delays_use_phase_appropriate_timeout_category(self) -> None:
        config = ScenarioConfig(scenario=Scenario.DELAYED_HEADERS, delay_seconds=0.3)
        with ScenarioServer(config) as server:
            timeouts = TimeoutConfig(
                connect_seconds=2,
                request_seconds=0.1,
                pool_acquire_seconds=5,
            )
            _, samples = _run(server, request_count=1, timeouts=timeouts)
        self.assertEqual(samples[0].error_category, ErrorCategory.READ_TIMEOUT)
        self.assertNotEqual(samples[0].error_category, ErrorCategory.CONNECT_TIMEOUT)
        self.assertNotEqual(samples[0].error_category, ErrorCategory.WRITE_TIMEOUT)


class FailureScenarioTests(unittest.TestCase):
    def test_forced_close_succeeds_without_reuse(self) -> None:
        config = ScenarioConfig(scenario=Scenario.FORCED_CLOSE, body=b"ok")
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=2)
        self.assertEqual(len(samples), 2)
        for sample in samples:
            self.assertEqual(sample.outcome, Outcome.SUCCESS)
            self.assertEqual(sample.status_code, 200)
            self.assertFalse(sample.connection_reused)
            self.assertIsNone(sample.error_category)

    def test_truncated_body_is_early_close_failure(self) -> None:
        config = ScenarioConfig(scenario=Scenario.TRUNCATED, body=b"ok")
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=1)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.CONNECTION_CLOSED_EARLY)
        self.assertIsNone(sample.status_code)

    def test_abrupt_reset_is_transport_failure(self) -> None:
        # Loopback often merges the RST into clean EOF, so the client
        # may file early close instead of reset. Both are transport
        # failures. The strict reset mapping is pinned by the fake
        # session test below.
        config = ScenarioConfig(scenario=Scenario.ABRUPT_RESET, body=b"ok")
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=2)
        self.assertEqual(len(samples), 2)
        allowed = {
            ErrorCategory.CONNECTION_RESET,
            ErrorCategory.CONNECTION_CLOSED_EARLY,
            ErrorCategory.TRANSPORT_FAILURE,
        }
        for sample in samples:
            self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
            self.assertIn(sample.error_category, allowed)
            self.assertIsNone(sample.status_code)

    def test_connection_reset_uses_existing_error_taxonomy(self) -> None:
        class ResetSession:
            reusable = True
            last_timing = None

            def execute(self, request, deadline_ns=None):
                raise ConnectionReset("connection reset by peer")

            def close(self) -> None:
                pass

        origin = Origin("http", "127.0.0.1", 80)
        plan = TestPlan(
            origin=origin,
            request=HttpRequest("GET", "/"),
            request_count=1,
            workers=1,
            max_connections=1,
        )
        pool = ConnectionPool(
            origin, 1, TimeoutConfig(), session_factory=ResetSession
        )
        (sample,) = WorkExecutor(plan, pool).run()
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.CONNECTION_RESET)

    def test_intermittent_500_remains_http_outcome(self) -> None:
        config = ScenarioConfig(scenario=Scenario.INTERMITTENT_500)
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=4)
        self.assertEqual(len(samples), 4)
        http_errors = [s for s in samples if s.outcome == Outcome.HTTP_ERROR]
        successes = [s for s in samples if s.outcome == Outcome.SUCCESS]
        self.assertEqual(len(http_errors), 2)
        self.assertEqual(len(successes), 2)
        for sample in http_errors:
            self.assertEqual(sample.status_code, 500)
            self.assertIsNone(sample.error_category)
        for sample in successes:
            self.assertEqual(sample.status_code, 200)


class MalformedScenarioTests(unittest.TestCase):
    def test_bad_status_line_is_protocol_failure(self) -> None:
        (sample,) = _run_against_raw(b"HTTP/2 200 OK\r\n\r\n")
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.PROTOCOL_ERROR)
        self.assertIsNone(sample.status_code)

    def test_header_without_colon_is_protocol_failure(self) -> None:
        (sample,) = _run_against_raw(b"HTTP/1.1 200 OK\r\nBroken\r\n\r\n")
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.INVALID_HEADERS)
        self.assertIsNone(sample.status_code)

    def test_bad_chunk_size_is_framing_failure(self) -> None:
        (sample,) = _run_against_raw(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZZ\r\n"
        )
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.INVALID_BODY_FRAMING)
        self.assertIsNone(sample.status_code)


class ResetVisibilityTests(unittest.TestCase):
    def test_abrupt_reset_sends_partial_before_close(self) -> None:
        body = b"ok"
        config = ScenarioConfig(scenario=Scenario.ABRUPT_RESET, body=body)
        with ScenarioServer(config) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                connection.settimeout(2)
                connection.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                first = connection.recv(4096)
                self.assertIn(b"Content-Length: 66", first)
                self.assertIn(body[:1], first)
                try:
                    second = connection.recv(4096)
                except OSError:
                    return
                # RST merged into EOF on some platforms.
                self.assertEqual(second, b"")

    def test_send_tcp_reset_arms_linger_zero(self) -> None:
        from test_server.server import _RESET_DECLARED_OVERHEAD, _send_tcp_reset

        self.assertEqual(_RESET_DECLARED_OVERHEAD, 64)

        calls: dict = {}

        class FakeSocket:
            def setsockopt(self, level, name, value) -> None:
                calls["setsockopt"] = (level, name, value)

            def shutdown(self, how) -> None:
                calls["shutdown"] = how

        fake = FakeSocket()
        _send_tcp_reset(fake)  # type: ignore[arg-type]
        self.assertEqual(
            calls["setsockopt"],
            (socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)),
        )
        self.assertEqual(calls["shutdown"], socket.SHUT_RDWR)


class SizeLimitTests(unittest.TestCase):
    def test_large_response_within_limits_succeeds(self) -> None:
        config = ScenarioConfig(
            scenario=Scenario.LARGE_RESPONSE, large_body_bytes=65536
        )
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].outcome, Outcome.SUCCESS)
        self.assertEqual(samples[0].bytes_received, 65536)

    def test_large_response_beyond_limits_is_rejected(self) -> None:
        config = ScenarioConfig(
            scenario=Scenario.LARGE_RESPONSE, large_body_bytes=65536
        )
        limits = SafetyLimits(max_response_body_bytes=1024)
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=1, limits=limits)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.outcome, Outcome.TRANSPORT_ERROR)
        self.assertEqual(sample.error_category, ErrorCategory.RESPONSE_TOO_LARGE)

    def test_parser_buffer_boundaries_with_one_byte_fragments(self) -> None:
        config = ScenarioConfig(scenario=Scenario.FIXED, body=b"hello")
        with ScenarioServer(config) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                connection.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                raw = b""
                while b"hello" not in raw:
                    fragment = connection.recv(4096)
                    if not fragment:
                        break
                    raw += fragment
        response = ResponseParser().parse_response(
            ChunkReader(raw, chunk_size=1), SafetyLimits()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body_bytes, 5)
        self.assertTrue(response.connection_reusable)


class ExecutionSemanticsTests(unittest.TestCase):
    def test_fixed_count_execution(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"ok")) as server:
            _, samples = _run(server, request_count=3)
        self.assertEqual(len(samples), 3)
        self.assertTrue(all(s.outcome == Outcome.SUCCESS for s in samples))

    def test_fixed_duration_execution(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"ok")) as server:
            _, samples = _run(
                server, request_count=None, duration_seconds=0.3
            )
        self.assertGreaterEqual(len(samples), 1)
        self.assertTrue(all(s.outcome == Outcome.SUCCESS for s in samples))

    def test_keep_alive_reuse(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"ok")) as server:
            _, samples = _run(server, request_count=2)
        self.assertEqual(len(samples), 2)
        self.assertFalse(samples[0].connection_reused)
        self.assertTrue(samples[1].connection_reused)

    def test_chunked_response_succeeds(self) -> None:
        config = ScenarioConfig(scenario=Scenario.CHUNKED, body=b"hello-chunked")
        with ScenarioServer(config) as server:
            _, samples = _run(server, request_count=1)
        self.assertEqual(samples[0].outcome, Outcome.SUCCESS)
        self.assertEqual(samples[0].bytes_received, len(b"hello-chunked"))


class RunnerIntegrationTests(unittest.TestCase):
    def test_cli_runner_fixed_count_against_scenario_server(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"ok")) as server:
            self.assertTrue(server.url.startswith("http://"))
            plan = load_plan([server.url, "--count", "2"])
            self.assertEqual(plan.origin.scheme, "http")
            output = io.StringIO()
            errors = io.StringIO()
            code = run_plan(plan, stdout=output, stderr=errors)
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("Attempts:", rendered)
        self.assertIn("Responses:", rendered)
        self.assertEqual(errors.getvalue(), "")

    def test_runner_fixed_duration_against_scenario_server(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"ok")) as server:
            plan = load_plan([server.url, "--duration", "0.3"])
            output = io.StringIO()
            errors = io.StringIO()
            code = run_plan(plan, stdout=output, stderr=errors)
        self.assertEqual(code, 0)
        self.assertIn("Attempts:", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_runner_reports_http_500_as_http_error_not_transport_failure(self) -> None:
        with ScenarioServer(ScenarioConfig(scenario=Scenario.INTERMITTENT_500)) as server:
            plan = load_plan([server.url, "--count", "2", "--format", "json"])
            output = io.StringIO()
            errors = io.StringIO()
            code = run_plan(plan, stdout=output, stderr=errors)
        import json

        payload = json.loads(output.getvalue())
        # One success and one HTTP 500: completed with errors, not a crash.
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"]["http_errors"], 1)
        self.assertEqual(payload["results"]["successes"], 1)
        self.assertEqual(payload["results"]["status_codes"]["500"], 1)
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
