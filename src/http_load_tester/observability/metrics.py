"""Owner-side metrics aggregation for completed HTTP attempts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from ..domain.errors import ErrorCategory
from ..domain.models import Outcome, ResultSample


DEFAULT_PERCENTILES = (50, 90, 95, 99)


def percentile(values: Iterable[int], percent: int) -> float | None:
    """Return an exact linearly interpolated percentile in source units."""
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise ValueError("percent must be an integer between 0 and 100")
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _validate_percentiles(percentiles: Iterable[int]) -> tuple[int, ...]:
    values = tuple(percentiles)
    if not values or any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100
        for value in values
    ):
        raise ValueError("percentiles must contain integers between 0 and 100")
    if tuple(sorted(set(values))) != values:
        raise ValueError("percentiles must be sorted and unique")
    return values


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    total_attempts: int
    success_count: int
    http_error_count: int
    failure_count: int
    status_counts: Mapping[int, int]
    outcome_counts: Mapping[Outcome, int]
    error_counts: Mapping[ErrorCategory, int]
    request_latency_percentiles_ns: Mapping[int, float | None]
    end_to_end_latency_percentiles_ns: Mapping[int, float | None]
    pool_wait_percentiles_ns: Mapping[int, float | None]
    time_to_first_byte_percentiles_ns: Mapping[int, float | None]
    throughput_requests_per_second: float | None
    bytes_sent: int
    bytes_received: int
    connection_reuse_ratio: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_counts", MappingProxyType(dict(self.status_counts)))
        object.__setattr__(self, "outcome_counts", MappingProxyType(dict(self.outcome_counts)))
        object.__setattr__(self, "error_counts", MappingProxyType(dict(self.error_counts)))
        for field_name in (
            "request_latency_percentiles_ns",
            "end_to_end_latency_percentiles_ns",
            "pool_wait_percentiles_ns",
            "time_to_first_byte_percentiles_ns",
        ):
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(dict(getattr(self, field_name))),
            )

    @property
    def error_rate(self) -> float:
        return self.failure_count / self.total_attempts if self.total_attempts else 0.0

    @property
    def response_count(self) -> int:
        return self.success_count + self.http_error_count

    @property
    def transport_error_count(self) -> int:
        return self.outcome_counts.get(Outcome.TRANSPORT_ERROR, 0)

    @property
    def protocol_error_count(self) -> int:
        return self.outcome_counts.get(Outcome.PROTOCOL_ERROR, 0)

    @property
    def timeout_count(self) -> int:
        return self.outcome_counts.get(Outcome.TIMEOUT, 0)

    @property
    def cancelled_count(self) -> int:
        return self.outcome_counts.get(Outcome.CANCELLED, 0)

    @property
    def pool_acquire_timeout_count(self) -> int:
        return self.error_counts.get(ErrorCategory.POOL_ACQUIRE_TIMEOUT, 0)

class MetricsCollector:
    """Aggregate samples in one reporting/owner thread."""

    def __init__(self, *, percentiles: Iterable[int] = DEFAULT_PERCENTILES) -> None:
        self._percentiles = _validate_percentiles(percentiles)
        self._samples: list[ResultSample] = []

    @property
    def percentiles(self) -> tuple[int, ...]:
        return self._percentiles

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def add(self, sample: ResultSample) -> None:
        if not isinstance(sample, ResultSample):
            raise TypeError("sample must be a ResultSample")
        self._samples.append(sample)

    def extend(self, samples: Iterable[ResultSample]) -> None:
        for sample in samples:
            self.add(sample)

    def snapshot(self, *, run_duration_ns: int | None = None) -> MetricsSnapshot:
        samples = tuple(self._samples)
        status_counts = Counter(
            sample.status_code for sample in samples if sample.status_code is not None
        )
        outcome_counts = Counter(sample.outcome for sample in samples)
        error_counts = Counter(
            sample.error_category
            for sample in samples
            if sample.error_category is not None
        )
        request_latencies = [
            sample.request_latency_ns for sample in samples
            if sample.request_latency_ns is not None
        ]
        end_to_end_latencies = [sample.end_to_end_latency_ns for sample in samples]
        pool_waits = [
            sample.pool_wait_ns for sample in samples if sample.pool_wait_ns is not None
        ]
        first_bytes = [
            sample.time_to_first_byte_ns
            for sample in samples
            if sample.time_to_first_byte_ns is not None
        ]
        duration_ns = run_duration_ns
        if duration_ns is None and samples:
            duration_ns = max(sample.completion_ns for sample in samples) - min(
                sample.scheduled_ns for sample in samples
            )
        if duration_ns is not None and duration_ns < 0:
            raise ValueError("run_duration_ns must be non-negative")
        throughput = (
            len(samples) / (duration_ns / 1_000_000_000)
            if duration_ns and duration_ns > 0
            else (0.0 if duration_ns == 0 and samples else None)
        )
        acquired = [sample for sample in samples if sample.pool_acquire_end_ns is not None]
        reuse_ratio = (
            sum(sample.connection_reused for sample in acquired) / len(acquired)
            if acquired
            else None
        )
        return MetricsSnapshot(
            total_attempts=len(samples),
            success_count=outcome_counts[Outcome.SUCCESS],
            http_error_count=outcome_counts[Outcome.HTTP_ERROR],
            failure_count=len(samples) - outcome_counts[Outcome.SUCCESS],
            status_counts=status_counts,
            outcome_counts=outcome_counts,
            error_counts=error_counts,
            request_latency_percentiles_ns={
                value: percentile(request_latencies, value) for value in self._percentiles
            },
            end_to_end_latency_percentiles_ns={
                value: percentile(end_to_end_latencies, value) for value in self._percentiles
            },
            pool_wait_percentiles_ns={
                value: percentile(pool_waits, value) for value in self._percentiles
            },
            time_to_first_byte_percentiles_ns={
                value: percentile(first_bytes, value) for value in self._percentiles
            },
            throughput_requests_per_second=throughput,
            bytes_sent=sum(sample.bytes_sent for sample in samples),
            bytes_received=sum(sample.bytes_received for sample in samples),
            connection_reuse_ratio=reuse_ratio,
        )
