"""Integration tests for fault injection during real workload execution."""

import unittest

from http_load_tester.domain.models import (
    FaultMode,
    FaultPolicy,
    HttpRequest,
    Outcome,
    TestPlan,
    TimeoutConfig,
)
from http_load_tester.http.session import Http1Session
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.pool.connection_pool import ConnectionPool
from http_load_tester.transport.tcp import TcpTransport
from test_server import ScenarioConfig, ScenarioServer


class FaultExecutionIntegrationTests(unittest.TestCase):
    def test_executor_with_connection_churn_prevents_reuse(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"test-response")) as server:
            host, port = server.address
            from http_load_tester.domain.models import Origin
            origin = Origin("http", host, port)
            plan = TestPlan(
                origin=origin,
                request=HttpRequest("GET", "/"),
                request_count=3,
                workers=1,
                max_connections=1,
                fault_policy=FaultPolicy(
                    mode=FaultMode.CONNECTION_CHURN,
                    seed=42,
                ),
            )
            pool = ConnectionPool(
                origin,
                1,
                TimeoutConfig(connect_seconds=1, request_seconds=1),
                session_factory=lambda: Http1Session(
                    origin,
                    TcpTransport.connect(origin, TimeoutConfig(connect_seconds=1, request_seconds=1)),
                ),
            )
            executor = WorkExecutor(plan, pool)
            samples = executor.run()

            self.assertEqual(len(samples), 3)
            self.assertTrue(all(s.outcome == Outcome.SUCCESS for s in samples))
            self.assertTrue(all(s.fault_applied for s in samples))
            self.assertTrue(all(s.fault_mode == "connection_churn" for s in samples))
            # With connection churn, every attempt forces a new connection
            self.assertTrue(all(s.connection_reused is False for s in samples))

    def test_executor_with_randomized_body(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"echo")) as server:
            host, port = server.address
            from http_load_tester.domain.models import Origin
            origin = Origin("http", host, port)
            plan = TestPlan(
                origin=origin,
                request=HttpRequest("POST", "/", body=b"initial"),
                request_count=2,
                workers=1,
                max_connections=1,
                fault_policy=FaultPolicy(
                    mode=FaultMode.RANDOMIZED_BODY,
                    randomized_body_bytes=16,
                    seed=99,
                ),
            )
            pool = ConnectionPool(
                origin,
                1,
                TimeoutConfig(connect_seconds=1, request_seconds=1),
                session_factory=lambda: Http1Session(
                    origin,
                    TcpTransport.connect(origin, TimeoutConfig(connect_seconds=1, request_seconds=1)),
                ),
            )
            executor = WorkExecutor(plan, pool)
            samples = executor.run()

            self.assertEqual(len(samples), 2)
            self.assertTrue(all(s.outcome == Outcome.SUCCESS for s in samples))
            self.assertTrue(all(s.fault_applied for s in samples))
            self.assertTrue(all(s.fault_mode == "randomized_body" for s in samples))

    def test_executor_with_abort_after_headers(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"echo")) as server:
            host, port = server.address
            from http_load_tester.domain.models import Origin
            origin = Origin("http", host, port)
            plan = TestPlan(
                origin=origin,
                request=HttpRequest("GET", "/"),
                request_count=2,
                workers=1,
                max_connections=1,
                fault_policy=FaultPolicy(
                    mode=FaultMode.ABORT_AFTER_HEADERS,
                    seed=1,
                ),
            )
            pool = ConnectionPool(
                origin,
                1,
                TimeoutConfig(connect_seconds=1, request_seconds=1),
                session_factory=lambda: Http1Session(
                    origin,
                    TcpTransport.connect(origin, TimeoutConfig(connect_seconds=1, request_seconds=1)),
                ),
            )
            executor = WorkExecutor(plan, pool)
            samples = executor.run()

            self.assertEqual(len(samples), 2)
            self.assertTrue(all(s.outcome == Outcome.TRANSPORT_ERROR for s in samples))
            self.assertTrue(all(s.fault_applied for s in samples))
            self.assertTrue(all(s.fault_mode == "abort_after_headers" for s in samples))


if __name__ == "__main__":
    unittest.main()
