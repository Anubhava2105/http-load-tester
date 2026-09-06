"""Owner-side metrics aggregation for completed HTTP attempts."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import Iterable, Mapping

from ..domain.errors import ErrorCategory
from ..domain.models import MetricsMode, Outcome, ResultSample


DEFAULT_PERCENTILES = (50, 90, 95, 99)

MAX_RESERVOIR_SIZE = 1_000_000


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


def _normalize_mode(mode: MetricsMode | str) -> MetricsMode:
    if isinstance(mode, MetricsMode):
        return mode
    if isinstance(mode, str):
        try:
            return MetricsMode(mode)
        except ValueError:
            pass
    raise ValueError("mode must be 'exact' or 'bounded'")


def _validate_reservoir_size(reservoir_size: int) -> int:
    if (
        isinstance(reservoir_size, bool)
        or not isinstance(reservoir_size, int)
        or not 1 <= reservoir_size <= MAX_RESERVOIR_SIZE
    ):
        raise ValueError(
            f"reservoir_size must be an integer between 1 and {MAX_RESERVOIR_SIZE}"
        )
    return reservoir_size


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
    fault_applied_count: int = 0
    fault_mode_counts: Mapping[str, int] = MappingProxyType({})
    metrics_mode: str = MetricsMode.EXACT.value
    approximate_percentiles: bool = False
    reservoir_size: int | None = None

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
        object.__setattr__(
            self,
            "fault_mode_counts",
            MappingProxyType(dict(self.fault_mode_counts)),
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
    """Aggregate samples in one reporting/owner thread.

    Exact mode retains every sample and computes exact percentiles.
    Bounded mode keeps a fixed recent window of at most
    ``reservoir_size`` values per latency stream, so percentile inputs
    stay within ``4 * reservoir_size`` values no matter how many
    attempts run. Counters, byte totals, and reuse ratios stay exact in
    both modes; only percentiles are approximate in bounded mode.
    """

    def __init__(
        self,
        *,
        percentiles: Iterable[int] = DEFAULT_PERCENTILES,
        mode: MetricsMode | str = MetricsMode.EXACT,
        reservoir_size: int = 1024,
    ) -> None:
        self._percentiles = _validate_percentiles(percentiles)
        self._mode = _normalize_mode(mode)
        self._reservoir_size = _validate_reservoir_size(reservoir_size)
        self._lock = Lock()
        self._samples: list[ResultSample] = []
        self._total_attempts = 0
        self._status_counts: Counter[int] = Counter()
        self._outcome_counts: Counter[Outcome] = Counter()
        self._error_counts: Counter[ErrorCategory] = Counter()
        self._fault_mode_counts: Counter[str] = Counter()
        self._bytes_sent = 0
        self._bytes_received = 0
        self._acquired = 0
        self._reused = 0
        self._fault_applied_count = 0
        self._min_scheduled_ns: int | None = None
        self._max_completion_ns: int | None = None
        self._request_window: deque[int] = deque(maxlen=self._reservoir_size)
        self._end_to_end_window: deque[int] = deque(maxlen=self._reservoir_size)
        self._pool_wait_window: deque[int] = deque(maxlen=self._reservoir_size)
        self._first_byte_window: deque[int] = deque(maxlen=self._reservoir_size)

    @property
    def percentiles(self) -> tuple[int, ...]:
        return self._percentiles

    @property
    def mode(self) -> MetricsMode:
        return self._mode

    @property
    def reservoir_size(self) -> int:
        return self._reservoir_size

    @property
    def approximate(self) -> bool:
        return self._mode is MetricsMode.BOUNDED

    @property
    def sample_count(self) -> int:
        with self._lock:
            if self._mode is MetricsMode.EXACT:
                return len(self._samples)
            return self._total_attempts

    def retained_counts(self) -> dict[str, int]:
        """Number of latency values held per stream right now."""
        with self._lock:
            if self._mode is MetricsMode.EXACT:
                request = [s.request_latency_ns for s in self._samples]
                end_to_end = [s.end_to_end_latency_ns for s in self._samples]
                pool_wait = [s.pool_wait_ns for s in self._samples]
                first_byte = [s.time_to_first_byte_ns for s in self._samples]
                return {
                    "request": sum(1 for value in request if value is not None),
                    "end_to_end": len(end_to_end),
                    "pool_wait": sum(1 for value in pool_wait if value is not None),
                    "time_to_first_byte": sum(
                        1 for value in first_byte if value is not None
                    ),
                }
            return {
                "request": len(self._request_window),
                "end_to_end": len(self._end_to_end_window),
                "pool_wait": len(self._pool_wait_window),
                "time_to_first_byte": len(self._first_byte_window),
            }

    def add(self, sample: ResultSample) -> None:
        if not isinstance(sample, ResultSample):
            raise TypeError("sample must be a ResultSample")
        with self._lock:
            if self._mode is MetricsMode.EXACT:
                self._samples.append(sample)
                return
            self._accumulate_bounded(sample)

    def extend(self, samples: Iterable[ResultSample]) -> None:
        for sample in samples:
            self.add(sample)

    def _accumulate_bounded(self, sample: ResultSample) -> None:
        self._total_attempts += 1
        if sample.status_code is not None:
            self._status_counts[sample.status_code] += 1
        self._outcome_counts[sample.outcome] += 1
        if sample.error_category is not None:
            self._error_counts[sample.error_category] += 1
        self._bytes_sent += sample.bytes_sent
        self._bytes_received += sample.bytes_received
        if sample.pool_acquire_end_ns is not None:
            self._acquired += 1
            if sample.connection_reused:
                self._reused += 1
        if sample.fault_applied:
            self._fault_applied_count += 1
        if sample.fault_mode is not None:
            self._fault_mode_counts[sample.fault_mode] += 1
        if self._min_scheduled_ns is None or sample.scheduled_ns < self._min_scheduled_ns:
            self._min_scheduled_ns = sample.scheduled_ns
        if (
            self._max_completion_ns is None
            or sample.completion_ns > self._max_completion_ns
        ):
            self._max_completion_ns = sample.completion_ns
        if sample.request_latency_ns is not None:
            self._request_window.append(sample.request_latency_ns)
        self._end_to_end_window.append(sample.end_to_end_latency_ns)
        if sample.pool_wait_ns is not None:
            self._pool_wait_window.append(sample.pool_wait_ns)
        if sample.time_to_first_byte_ns is not None:
            self._first_byte_window.append(sample.time_to_first_byte_ns)

    def snapshot(self, *, run_duration_ns: int | None = None) -> MetricsSnapshot:
        with self._lock:
            if self._mode is MetricsMode.EXACT:
                return self._exact_snapshot(run_duration_ns=run_duration_ns)
            return self._bounded_snapshot(run_duration_ns=run_duration_ns)

    def _exact_snapshot(self, *, run_duration_ns: int | None) -> MetricsSnapshot:
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
        fault_applied_count = sum(
            1 for sample in samples if sample.fault_applied
        )
        fault_mode_counter: Counter[str] = Counter(
            sample.fault_mode
            for sample in samples
            if sample.fault_mode is not None
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
            fault_applied_count=fault_applied_count,
            fault_mode_counts=fault_mode_counter,
            metrics_mode=MetricsMode.EXACT.value,
            approximate_percentiles=False,
            reservoir_size=None,
        )

    def _bounded_snapshot(self, *, run_duration_ns: int | None) -> MetricsSnapshot:
        total = self._total_attempts
        success_count = self._outcome_counts[Outcome.SUCCESS]
        http_error_count = self._outcome_counts[Outcome.HTTP_ERROR]
        duration_ns = run_duration_ns
        if duration_ns is None and total:
            assert self._min_scheduled_ns is not None
            assert self._max_completion_ns is not None
            duration_ns = self._max_completion_ns - self._min_scheduled_ns
        if duration_ns is not None and duration_ns < 0:
            raise ValueError("run_duration_ns must be non-negative")
        throughput = (
            total / (duration_ns / 1_000_000_000)
            if duration_ns and duration_ns > 0
            else (0.0 if duration_ns == 0 and total else None)
        )
        reuse_ratio = self._reused / self._acquired if self._acquired else None
        return MetricsSnapshot(
            total_attempts=total,
            success_count=success_count,
            http_error_count=http_error_count,
            failure_count=total - success_count,
            status_counts=Counter(self._status_counts),
            outcome_counts=Counter(self._outcome_counts),
            error_counts=Counter(self._error_counts),
            request_latency_percentiles_ns={
                value: percentile(self._request_window, value)
                for value in self._percentiles
            },
            end_to_end_latency_percentiles_ns={
                value: percentile(self._end_to_end_window, value)
                for value in self._percentiles
            },
            pool_wait_percentiles_ns={
                value: percentile(self._pool_wait_window, value)
                for value in self._percentiles
            },
            time_to_first_byte_percentiles_ns={
                value: percentile(self._first_byte_window, value)
                for value in self._percentiles
            },
            throughput_requests_per_second=throughput,
            bytes_sent=self._bytes_sent,
            bytes_received=self._bytes_received,
            connection_reuse_ratio=reuse_ratio,
            fault_applied_count=self._fault_applied_count,
            fault_mode_counts=Counter(self._fault_mode_counts),
            metrics_mode=MetricsMode.BOUNDED.value,
            approximate_percentiles=True,
            reservoir_size=self._reservoir_size,
        )
