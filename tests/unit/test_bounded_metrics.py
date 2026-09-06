"""Bounded latency metrics collection (Stage 17)."""

import json
import threading
import unittest

from http_load_tester.application.config import load_plan
from http_load_tester.domain.errors import ConfigurationError
from http_load_tester.domain.models import (
    MetricsConfig,
    MetricsMode,
    Outcome,
    ResultSample,
)
from http_load_tester.domain.errors import ErrorCategory
from http_load_tester.observability.metrics import MetricsCollector, percentile
from http_load_tester.observability.renderers import render_json, render_terminal
from http_load_tester.observability.report import Report


def _success_sample(
    index: int,
    *,
    request_latency: int = 100,
    first_byte_gap: int = 20,
    pool_wait: int = 50,
) -> ResultSample:
    scheduled = index * 1000
    pool_end = scheduled + pool_wait
    write_start = pool_end
    write_end = write_start + request_latency - first_byte_gap
    first_byte = write_end + first_byte_gap
    return ResultSample(
        request_id=f"request-{index}",
        scheduled_ns=scheduled,
        worker_start_ns=scheduled,
        pool_acquire_start_ns=scheduled,
        pool_acquire_end_ns=pool_end,
        write_start_ns=write_start,
        write_end_ns=write_end,
        first_byte_ns=first_byte,
        completion_ns=first_byte,
        status_code=200,
        outcome=Outcome.SUCCESS,
        error_category=None,
        bytes_sent=10,
        bytes_received=40,
    )


def _timeout_sample(index: int) -> ResultSample:
    scheduled = index * 1000
    return ResultSample(
        request_id=f"request-{index}",
        scheduled_ns=scheduled,
        worker_start_ns=scheduled,
        pool_acquire_start_ns=scheduled,
        pool_acquire_end_ns=scheduled + 5,
        write_start_ns=None,
        write_end_ns=None,
        first_byte_ns=None,
        completion_ns=scheduled + 500,
        status_code=None,
        outcome=Outcome.TIMEOUT,
        error_category=ErrorCategory.READ_TIMEOUT,
    )


class ExactDefaultTests(unittest.TestCase):
    def test_exact_remains_the_default_with_exact_percentiles(self) -> None:
        collector = MetricsCollector()
        self.assertEqual(collector.mode, MetricsMode.EXACT)
        self.assertFalse(collector.approximate)
        collector.extend(_success_sample(i, request_latency=lat) for i, lat in enumerate([100, 200, 300, 400]))
        snapshot = collector.snapshot(run_duration_ns=1_000_000_000)
        self.assertEqual(snapshot.metrics_mode, "exact")
        self.assertFalse(snapshot.approximate_percentiles)
        self.assertIsNone(snapshot.reservoir_size)
        self.assertEqual(snapshot.request_latency_percentiles_ns[50], 250.0)
        self.assertEqual(snapshot.request_latency_percentiles_ns[95], 385.0)


class BoundedWindowTests(unittest.TestCase):
    def test_empty_bounded_snapshot_is_explicit(self) -> None:
        snapshot = MetricsCollector(
            mode=MetricsMode.BOUNDED, reservoir_size=8
        ).snapshot()
        self.assertEqual(snapshot.total_attempts, 0)
        self.assertTrue(snapshot.approximate_percentiles)
        self.assertEqual(snapshot.metrics_mode, "bounded")
        self.assertEqual(snapshot.reservoir_size, 8)
        self.assertIsNone(snapshot.request_latency_percentiles_ns[50])
        self.assertIsNone(snapshot.end_to_end_latency_percentiles_ns[50])
        self.assertIsNone(snapshot.pool_wait_percentiles_ns[50])
        self.assertIsNone(snapshot.time_to_first_byte_percentiles_ns[50])
        self.assertIsNone(snapshot.connection_reuse_ratio)
        self.assertIsNone(snapshot.throughput_requests_per_second)

    def test_single_bounded_sample_reports_its_own_values(self) -> None:
        collector = MetricsCollector(mode="bounded", reservoir_size=8)
        collector.add(_success_sample(1, request_latency=120))
        snapshot = collector.snapshot(run_duration_ns=1_000_000_000)
        self.assertEqual(snapshot.total_attempts, 1)
        self.assertTrue(snapshot.approximate_percentiles)
        self.assertEqual(snapshot.request_latency_percentiles_ns[50], 120.0)
        self.assertEqual(snapshot.pool_wait_percentiles_ns[50], 50.0)
        self.assertEqual(snapshot.time_to_first_byte_percentiles_ns[50], 20.0)
        self.assertEqual(snapshot.end_to_end_latency_percentiles_ns[50], 170.0)

    def test_multi_sample_percentiles_cover_only_the_recent_window(self) -> None:
        collector = MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=4)
        latencies = [110, 120, 130, 140, 150, 160, 170, 180]
        for index, latency in enumerate(latencies):
            collector.add(_success_sample(index, request_latency=latency))
        snapshot = collector.snapshot(run_duration_ns=1_000_000_000)
        window = [150, 160, 170, 180]
        self.assertEqual(
            snapshot.request_latency_percentiles_ns[50], percentile(window, 50)
        )
        self.assertNotEqual(
            snapshot.request_latency_percentiles_ns[50],
            percentile(latencies, 50),
        )
        self.assertEqual(collector.retained_counts()["request"], 4)

    def test_bounded_counters_stay_exact(self) -> None:
        collector = MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=4)
        for index in range(6):
            collector.add(_success_sample(index))
        for index in range(6, 8):
            collector.add(_timeout_sample(index))
        snapshot = collector.snapshot(run_duration_ns=1_000_000_000)
        self.assertEqual(snapshot.total_attempts, 8)
        self.assertEqual(snapshot.success_count, 6)
        self.assertEqual(snapshot.failure_count, 2)
        self.assertEqual(snapshot.timeout_count, 2)
        self.assertEqual(snapshot.bytes_sent, 60)
        self.assertEqual(snapshot.bytes_received, 240)
        self.assertEqual(
            snapshot.error_counts, {ErrorCategory.READ_TIMEOUT: 2}
        )

    def test_memory_does_not_scale_with_attempt_count(self) -> None:
        collector = MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=32)
        for index in range(2000):
            collector.add(_success_sample(index, request_latency=100 + index % 7))
        retained = collector.retained_counts()
        self.assertEqual(collector.sample_count, 2000)
        for stream, count in retained.items():
            self.assertLessEqual(count, 32, stream)
        self.assertLessEqual(sum(retained.values()), 4 * 32)

    def test_concurrent_publication_is_safe(self) -> None:
        collector = MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=64)
        errors: list = []

        def _publish(offset: int) -> None:
            try:
                for index in range(250):
                    collector.add(_success_sample(offset + index))
            except Exception as exc:  # pragma: no cover - test fails below
                errors.append(exc)

        threads = [threading.Thread(target=_publish, args=(n * 250,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        snapshot = collector.snapshot(run_duration_ns=1_000_000_000)
        self.assertEqual(snapshot.total_attempts, 1000)
        for count in collector.retained_counts().values():
            self.assertLessEqual(count, 64)


class BoundedValidationTests(unittest.TestCase):
    def test_metrics_config_rejects_bad_values(self) -> None:
        with self.assertRaises(ConfigurationError):
            MetricsConfig(mode="bounded")  # type: ignore[arg-type]
        with self.assertRaises(ConfigurationError):
            MetricsConfig(mode=MetricsMode.BOUNDED, reservoir_size=0)
        with self.assertRaises(ConfigurationError):
            MetricsConfig(mode=MetricsMode.BOUNDED, reservoir_size=1_000_001)

    def test_collector_rejects_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            MetricsCollector(mode="histogram")
        with self.assertRaises(ValueError):
            MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=0)
        with self.assertRaises(ValueError):
            MetricsCollector(mode=MetricsMode.BOUNDED, reservoir_size=2_000_000)

    def test_cli_flags_select_bounded_mode(self) -> None:
        plan = load_plan(
            [
                "http://example.test/",
                "--count",
                "2",
                "--metrics-mode",
                "bounded",
                "--metrics-reservoir-size",
                "64",
            ]
        )
        self.assertEqual(plan.metrics.mode, MetricsMode.BOUNDED)
        self.assertEqual(plan.metrics.reservoir_size, 64)

    def test_cli_defaults_to_exact(self) -> None:
        plan = load_plan(["http://example.test/", "--count", "1"])
        self.assertEqual(plan.metrics.mode, MetricsMode.EXACT)

    def test_cli_rejects_bad_reservoir_size(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_plan(
                [
                    "http://example.test/",
                    "--count",
                    "1",
                    "--metrics-mode",
                    "bounded",
                    "--metrics-reservoir-size",
                    "0",
                ]
            )


class BoundedReportTests(unittest.TestCase):
    def _bounded_report(self) -> Report:
        plan = load_plan(
            [
                "http://example.test/",
                "--count",
                "2",
                "--metrics-mode",
                "bounded",
                "--metrics-reservoir-size",
                "16",
            ]
        )
        collector = MetricsCollector(
            mode=plan.metrics.mode, reservoir_size=plan.metrics.reservoir_size
        )
        collector.add(_success_sample(0))
        collector.add(_success_sample(1))
        return Report.from_plan(
            plan, collector.snapshot(run_duration_ns=1_000_000_000)
        )

    def test_json_marks_approximate_and_mode(self) -> None:
        payload = json.loads(render_json(self._bounded_report()))
        self.assertEqual(payload["results"]["metrics_mode"], "bounded")
        self.assertTrue(payload["results"]["approximate_percentiles"])
        self.assertEqual(payload["results"]["metrics_reservoir_size"], 16)
        self.assertEqual(payload["configuration"]["metrics_mode"], "bounded")
        self.assertEqual(payload["configuration"]["metrics_reservoir_size"], 16)

    def test_terminal_marks_approximate(self) -> None:
        rendered = render_terminal(self._bounded_report())
        self.assertIn("approximate", rendered)
        self.assertIn("reservoir 16", rendered)

    def test_exact_report_stays_unmarked(self) -> None:
        plan = load_plan(["http://example.test/", "--count", "1"])
        collector = MetricsCollector()
        collector.add(_success_sample(0))
        report = Report.from_plan(
            plan, collector.snapshot(run_duration_ns=1_000_000_000)
        )
        payload = json.loads(render_json(report))
        self.assertEqual(payload["results"]["metrics_mode"], "exact")
        self.assertFalse(payload["results"]["approximate_percentiles"])
        self.assertIn("Metrics:      exact", render_terminal(report))


if __name__ == "__main__":
    unittest.main()
