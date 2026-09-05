import threading
import unittest

from http_load_tester.domain.errors import ErrorCategory
from http_load_tester.domain.models import Outcome, ResultSample
from http_load_tester.observability.metrics import MetricsCollector, percentile
from http_load_tester.observability.samples import SampleCollector


def make_sample(
    request_id: str,
    *,
    scheduled_ns: int,
    completion_ns: int,
    write_start_ns: int | None,
    write_end_ns: int | None,
    first_byte_ns: int | None,
    pool_acquire_end_ns: int | None,
    outcome: Outcome,
    status_code: int | None = None,
    error_category: ErrorCategory | None = None,
    connection_reused: bool = False,
    bytes_sent: int = 0,
    bytes_received: int = 0,
) -> ResultSample:
    return ResultSample(
        request_id=request_id,
        scheduled_ns=scheduled_ns,
        worker_start_ns=scheduled_ns,
        pool_acquire_start_ns=scheduled_ns,
        pool_acquire_end_ns=pool_acquire_end_ns,
        write_start_ns=write_start_ns,
        write_end_ns=write_end_ns,
        first_byte_ns=first_byte_ns,
        completion_ns=completion_ns,
        status_code=status_code,
        outcome=outcome,
        error_category=error_category,
        connection_reused=connection_reused,
        bytes_sent=bytes_sent,
        bytes_received=bytes_received,
    )


class SampleCollectorTests(unittest.TestCase):
    def test_collects_concurrent_submissions_without_losing_ordered_items(self) -> None:
        collector = SampleCollector()
        samples = [
            make_sample(
                f"request-{index}",
                scheduled_ns=index,
                completion_ns=index + 10,
                write_start_ns=index + 1,
                write_end_ns=index + 2,
                first_byte_ns=index + 3,
                pool_acquire_end_ns=index,
                outcome=Outcome.SUCCESS,
                status_code=200,
            )
            for index in range(100)
        ]
        threads = [
            threading.Thread(
                target=lambda subset=subset: [collector.submit(sample) for sample in subset]
            )
            for subset in (samples[:25], samples[25:50], samples[50:75], samples[75:])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        collected = collector.collect()
        self.assertEqual({sample.request_id for sample in collected}, {sample.request_id for sample in samples})
        self.assertEqual(collector.collect(), ())

    def test_close_preserves_queued_samples_and_rejects_new_ones(self) -> None:
        collector = SampleCollector()
        sample = make_sample(
            "request-1",
            scheduled_ns=0,
            completion_ns=10,
            write_start_ns=1,
            write_end_ns=2,
            first_byte_ns=3,
            pool_acquire_end_ns=0,
            outcome=Outcome.SUCCESS,
            status_code=200,
        )
        collector.submit(sample)
        collector.close()
        self.assertEqual(collector.collect(), (sample,))
        with self.assertRaises(RuntimeError):
            collector.submit(sample)


class MetricsTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(percentile([100, 200, 300, 400], 50), 250.0)
        self.assertEqual(percentile([100, 200, 300, 400], 95), 385.0)
        self.assertIsNone(percentile([], 50))

    def test_snapshot_contains_breakdowns_latency_and_rates(self) -> None:
        metrics = MetricsCollector(percentiles=(50, 90))
        metrics.extend(
            [
                make_sample(
                    "request-1",
                    scheduled_ns=0,
                    completion_ns=500,
                    write_start_ns=100,
                    write_end_ns=200,
                    first_byte_ns=220,
                    pool_acquire_end_ns=50,
                    outcome=Outcome.SUCCESS,
                    status_code=200,
                    bytes_sent=10,
                    bytes_received=40,
                ),
                make_sample(
                    "request-2",
                    scheduled_ns=100,
                    completion_ns=800,
                    write_start_ns=300,
                    write_end_ns=400,
                    first_byte_ns=430,
                    pool_acquire_end_ns=200,
                    outcome=Outcome.HTTP_ERROR,
                    status_code=503,
                    connection_reused=True,
                    bytes_sent=10,
                    bytes_received=20,
                ),
                make_sample(
                    "request-3",
                    scheduled_ns=200,
                    completion_ns=900,
                    write_start_ns=None,
                    write_end_ns=None,
                    first_byte_ns=None,
                    pool_acquire_end_ns=None,
                    outcome=Outcome.TIMEOUT,
                    error_category=ErrorCategory.READ_TIMEOUT,
                ),
            ]
        )

        snapshot = metrics.snapshot(run_duration_ns=1_000_000_000)
        self.assertEqual(snapshot.total_attempts, 3)
        self.assertEqual(snapshot.success_count, 1)
        self.assertEqual(snapshot.http_error_count, 1)
        self.assertEqual(snapshot.failure_count, 2)
        self.assertEqual(snapshot.status_counts, {200: 1, 503: 1})
        self.assertAlmostEqual(snapshot.error_rate, 2 / 3)
        self.assertEqual(snapshot.request_latency_percentiles_ns[50], 450.0)
        self.assertEqual(snapshot.request_latency_percentiles_ns[90], 490.0)
        self.assertEqual(snapshot.end_to_end_latency_percentiles_ns[50], 700.0)
        self.assertEqual(snapshot.pool_wait_percentiles_ns[50], 75.0)
        self.assertEqual(snapshot.time_to_first_byte_percentiles_ns[50], 25.0)
        self.assertEqual(snapshot.throughput_requests_per_second, 3.0)
        self.assertEqual(snapshot.bytes_sent, 20)
        self.assertEqual(snapshot.bytes_received, 60)
        self.assertAlmostEqual(snapshot.connection_reuse_ratio, 0.5)
        self.assertEqual(snapshot.error_counts, {ErrorCategory.READ_TIMEOUT: 1})
