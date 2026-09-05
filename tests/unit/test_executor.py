import time
import unittest

from http_load_tester.domain.models import HttpRequest, Origin, TestPlan, TimeoutConfig
from http_load_tester.http.session import SessionTiming
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.pool.connection_pool import ConnectionPool


class FakeSession:
    def __init__(self) -> None:
        self.reusable = True
        self.last_timing = None
        self.executions = 0

    def execute(self, request: HttpRequest, deadline_ns: int | None = None):
        from http_load_tester.domain.models import HttpResponseSummary

        self.executions += 1
        now = time.monotonic_ns()
        self.last_timing = SessionTiming(now, now, now, now)
        return HttpResponseSummary(
            "HTTP/1.1",
            200,
            headers=(("Content-Length", "0"),),
            body_bytes=0,
            connection_reusable=True,
        )

    def close(self) -> None:
        self.reusable = False


class FakeFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


class ExecutorTests(unittest.TestCase):
    def test_fixed_count_publishes_valid_samples(self) -> None:
        origin = Origin("http", "example.test", 80)
        plan = TestPlan(
            origin=origin,
            request=HttpRequest("GET", "/"),
            request_count=4,
            workers=2,
            max_connections=2,
        )
        factory = FakeFactory()
        pool = ConnectionPool(
            origin,
            2,
            TimeoutConfig(),
            session_factory=factory,
        )
        executor = WorkExecutor(plan, pool)

        samples = executor.run()

        self.assertEqual(len(samples), 4)
        self.assertEqual(
            {sample.request_id for sample in samples},
            {"request-1", "request-2", "request-3", "request-4"},
        )
        self.assertTrue(all(sample.outcome.value == "success" for sample in samples))
        self.assertLessEqual(pool.live_connections, 2)
        self.assertEqual(sum(session.executions for session in factory.sessions), 4)
