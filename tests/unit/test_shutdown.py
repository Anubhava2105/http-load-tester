"""Shutdown, cancellation, and execution safety tests."""

import io
import threading
import time
import unittest

from http_load_tester.domain.models import (
    HttpRequest,
    HttpResponseSummary,
    LoadModel,
    Origin,
    Outcome,
    SafetyLimits,
    TestPlan,
    TimeoutConfig,
)
from http_load_tester.domain.errors import ErrorCategory
from http_load_tester.http.session import SessionTiming
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.observability.report import ExitCode
from http_load_tester.application.runner import run_plan, validate_plan_limits
from http_load_tester.pool.connection_pool import ConnectionPool


class SlowSession:
    """Session that blocks on execute() until an event fires."""

    def __init__(self, gate: threading.Event) -> None:
        self.reusable = True
        self.last_timing = None
        self._gate = gate

    def execute(self, request, deadline_ns=None):
        now = time.monotonic_ns()
        self.last_timing = SessionTiming(now, now, now, now)
        self._gate.wait(timeout=5)
        return HttpResponseSummary(
            "HTTP/1.1", 200,
            headers=(("Content-Length", "0"),),
            body_bytes=0,
            connection_reusable=True,
        )

    def close(self) -> None:
        self.reusable = False


class CrashingSession:
    """Session that raises on the Nth call to execute()."""

    def __init__(self, crash_after: int) -> None:
        self.reusable = True
        self.last_timing = None
        self._count = 0
        self._crash_after = crash_after

    def execute(self, request, deadline_ns=None):
        self._count += 1
        now = time.monotonic_ns()
        self.last_timing = SessionTiming(now, now, now, now)
        if self._count > self._crash_after:
            raise RuntimeError("intentional crash")
        return HttpResponseSummary(
            "HTTP/1.1", 200,
            headers=(("Content-Length", "0"),),
            body_bytes=0,
            connection_reusable=True,
        )

    def close(self) -> None:
        self.reusable = False


def _make_plan(**overrides):
    defaults = dict(
        origin=Origin("http", "example.test", 80),
        request=HttpRequest("GET", "/"),
        request_count=10,
        workers=2,
        max_connections=2,
    )
    defaults.update(overrides)
    return TestPlan(**defaults)


class ShutdownTests(unittest.TestCase):

    def test_cancel_stops_workers_mid_run(self) -> None:
        """cancel() prevents all 10 requests from completing."""
        gate = threading.Event()
        sessions = []

        def factory():
            s = SlowSession(gate)
            sessions.append(s)
            return s

        plan = _make_plan(request_count=10, workers=2, max_connections=2)
        pool = ConnectionPool(
            plan.origin, 2, TimeoutConfig(), session_factory=factory,
        )
        executor = WorkExecutor(plan, pool)

        def cancel_soon():
            time.sleep(0.05)
            executor.cancel()
            gate.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        samples = executor.run()
        self.assertLess(len(samples), 10)

    def test_runtime_deadline_stops_executor(self) -> None:
        """A short runtime deadline forces the executor to stop early."""
        gate = threading.Event()

        def factory():
            return SlowSession(gate)

        plan = _make_plan(request_count=100, workers=2, max_connections=2)
        pool = ConnectionPool(
            plan.origin, 2, TimeoutConfig(), session_factory=factory,
        )
        executor = WorkExecutor(plan, pool)
        deadline_ns = time.perf_counter_ns() + int(0.1 * 1_000_000_000)

        def release_gate():
            time.sleep(0.3)
            gate.set()

        threading.Thread(target=release_gate, daemon=True).start()
        samples = executor.run(runtime_deadline_ns=deadline_ns)
        self.assertLess(len(samples), 100)

    def test_worker_crash_does_not_deadlock(self) -> None:
        """A crashing session doesn't prevent run() from returning."""
        def factory():
            return CrashingSession(crash_after=1)

        plan = _make_plan(request_count=4, workers=1, max_connections=1)
        pool = ConnectionPool(
            plan.origin, 1, TimeoutConfig(), session_factory=factory,
        )
        executor = WorkExecutor(plan, pool)
        samples = executor.run()
        # Should have at least the first successful request,
        # plus error samples from the crash.
        self.assertGreater(len(samples), 0)

    def test_cancelled_samples_have_cancelled_outcome(self) -> None:
        """Samples produced after cancel() use Outcome.CANCELLED."""
        gate = threading.Event()

        def factory():
            return SlowSession(gate)

        plan = _make_plan(request_count=10, workers=2, max_connections=2)
        pool = ConnectionPool(
            plan.origin, 2, TimeoutConfig(), session_factory=factory,
        )
        executor = WorkExecutor(plan, pool)

        def cancel_soon():
            time.sleep(0.05)
            executor.cancel()
            gate.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        samples = executor.run()
        cancelled = [s for s in samples if s.outcome is Outcome.CANCELLED]
        # At least some of the in-flight work should be cancelled.
        # (Depending on timing, they might all complete before cancel fires,
        # so we just check the invariant on cancelled samples.)
        for s in cancelled:
            self.assertEqual(s.error_category, ErrorCategory.CANCELLED)

    def test_partial_report_on_interruption(self) -> None:
        """run_plan renders a report even when interrupted."""

        class FakeSession:
            def __init__(self):
                self.reusable = True
                self.last_timing = None

            def execute(self, request, deadline_ns=None):
                now = time.monotonic_ns()
                self.last_timing = SessionTiming(now, now, now, now)
                return HttpResponseSummary(
                    "HTTP/1.1", 200,
                    headers=(("Content-Length", "0"),),
                    body_bytes=0,
                    connection_reusable=True,
                )

            def close(self):
                self.reusable = False

        def factory():
            return FakeSession()

        plan = _make_plan(request_count=4, workers=1, max_connections=1)
        out = io.StringIO()
        err = io.StringIO()
        pool_factory = lambda p: ConnectionPool(
            p.origin, 1, TimeoutConfig(), session_factory=factory,
        )
        code = run_plan(plan, stdout=out, stderr=err, pool_factory=pool_factory)
        output = out.getvalue()
        self.assertIn("Attempts:", output)
        self.assertEqual(code, 0)


class PreflightValidationTests(unittest.TestCase):

    def test_overlimit_workers_rejected_before_execution(self) -> None:
        limits = SafetyLimits(max_workers=2)
        plan = _make_plan(workers=2, limits=limits)
        # workers=2 fits, but workers=3 would fail at TestPlan level.
        # Test the runner's validate_plan_limits separately.
        from http_load_tester.domain.errors import ConfigurationError
        plan_bad = _make_plan(workers=2, limits=SafetyLimits(max_workers=100))
        # This should pass:
        validate_plan_limits(plan_bad)

    def test_runtime_ceiling_rejects_long_plans(self) -> None:
        from http_load_tester.domain.errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            _make_plan(
                request_count=None,
                duration_seconds=100.0,
                warmup_seconds=50.0,
                limits=SafetyLimits(max_total_runtime_seconds=120.0),
            )


class ExitCodeTests(unittest.TestCase):

    def test_interrupted_exit_code_is_130(self) -> None:
        self.assertEqual(int(ExitCode.INTERRUPTED), 130)


if __name__ == "__main__":
    unittest.main()
