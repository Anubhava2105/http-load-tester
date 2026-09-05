"""Bounded worker executor for closed-loop HTTP attempts."""

from __future__ import annotations

from collections.abc import Callable
import threading

from ..domain.clock import Clock, MonotonicClock
from ..domain.errors import ErrorCategory, RawLoadError, TransportFailure
from ..domain.models import HttpRequest, LoadModel, Outcome, ResultSample, TestPlan
from ..http.request_encoder import encode_request
from ..http.session import Http1Session
from ..pool.connection_pool import ConnectionPool
from .scheduler import ClosedLoopScheduler, OpenLoopScheduler, ScheduledAttempt


SampleSink = Callable[[ResultSample], None]


class WorkExecutor:
    """Run a TestPlan with bounded workers and publish completed samples."""

    def __init__(
        self,
        plan: TestPlan,
        pool: ConnectionPool,
        *,
        clock: Clock | None = None,
        sample_sink: SampleSink | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(plan, TestPlan):
            raise TypeError("plan must be a TestPlan")
        if not isinstance(pool, ConnectionPool):
            raise TypeError("pool must be a ConnectionPool")
        self._plan = plan
        self._pool = pool
        self._clock = clock or MonotonicClock()
        scheduler_type = (
            OpenLoopScheduler
            if plan.load_model is LoadModel.OPEN_LOOP
            else ClosedLoopScheduler
        )
        self._scheduler = scheduler_type(plan, clock=self._clock, sleeper=sleeper)
        self._sample_sink = sample_sink
        self._samples: list[ResultSample] = []
        self._samples_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._run_lock = threading.Lock()
        self._running = False
        self._cancelled = threading.Event()
        self._encoded_request_bytes = len(encode_request(plan.request, plan.origin))

    @property
    def samples(self) -> tuple[ResultSample, ...]:
        with self._samples_lock:
            return tuple(self._samples)

    @property
    def scheduler(self) -> ClosedLoopScheduler | OpenLoopScheduler:
        return self._scheduler

    def run(self) -> tuple[ResultSample, ...]:
        with self._run_lock:
            if self._running:
                raise RuntimeError("executor is already running")
            self._running = True

        self._scheduler.start()
        self._scheduler.wait_until_start()
        try:
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    name=f"http-load-worker-{index + 1}",
                )
                for index in range(self._plan.workers)
            ]
            for thread in self._threads:
                thread.start()
            for thread in self._threads:
                thread.join()
            return self.samples
        finally:
            self._pool.close_all()
            with self._run_lock:
                self._running = False

    def stop(self) -> None:
        """Stop scheduling new work and let active attempts finish."""
        self._scheduler.stop()

    def cancel(self) -> None:
        """Stop scheduling and close the pool to interrupt future acquisitions."""
        self._cancelled.set()
        self._scheduler.stop()
        self._pool.close_all()

    def _worker(self) -> None:
        while not self._cancelled.is_set():
            attempt = self._scheduler.next_attempt()
            if attempt is None:
                return
            sample = self._run_attempt(attempt)
            with self._samples_lock:
                self._samples.append(sample)
            if self._sample_sink is not None:
                self._sample_sink(sample)

    def _run_attempt(self, attempt: ScheduledAttempt) -> ResultSample:
        worker_start_ns = self._clock.now_ns()
        pool_acquire_start_ns = self._clock.now_ns()
        pool_acquire_end_ns: int | None = None
        lease = None
        response = None
        error: RawLoadError | None = None
        timing = None
        request_deadline_ns = worker_start_ns + int(
            self._plan.timeouts.request_seconds * 1_000_000_000
        )
        acquire_deadline_ns = pool_acquire_start_ns + int(
            self._plan.timeouts.pool_acquire_seconds * 1_000_000_000
        )

        try:
            lease = self._pool.acquire(acquire_deadline_ns)
            pool_acquire_end_ns = self._clock.now_ns()
            session: Http1Session = lease.connection
            response = session.execute(self._plan.request, request_deadline_ns)
            timing = session.last_timing
        except RawLoadError as exc:
            error = exc
            if lease is not None:
                timing = lease.connection.last_timing
        except Exception as exc:
            error = TransportFailure("worker attempt failed", cause=exc)
            if lease is not None:
                timing = lease.connection.last_timing
        finally:
            if lease is not None:
                reusable = (
                    response.connection_reusable
                    if response is not None
                    else lease.connection.reusable
                )
                lease.release(reusable)

        completion_ns = self._clock.now_ns()
        write_start_ns = timing.write_start_ns if timing is not None else None
        write_end_ns = timing.write_end_ns if timing is not None else None
        first_byte_ns = timing.first_byte_ns if timing is not None else None
        bytes_sent = self._encoded_request_bytes if write_end_ns is not None else 0

        if response is not None:
            outcome = (
                Outcome.HTTP_ERROR
                if response.status_code >= 400
                else Outcome.SUCCESS
            )
            status_code = response.status_code
            error_category = None
            bytes_received = response.body_bytes
        else:
            timeout_categories = {
                ErrorCategory.CONNECT_TIMEOUT,
                ErrorCategory.WRITE_TIMEOUT,
                ErrorCategory.READ_TIMEOUT,
            }
            outcome = (
                Outcome.TIMEOUT
                if error is not None and error.category in timeout_categories
                else Outcome.TRANSPORT_ERROR
            )
            status_code = None
            error_category = (
                error.category
                if error is not None
                else ErrorCategory.TRANSPORT_FAILURE
            )
            bytes_received = 0

        return ResultSample(
            request_id=attempt.request_id,
            scheduled_ns=attempt.scheduled_ns,
            worker_start_ns=worker_start_ns,
            pool_acquire_start_ns=pool_acquire_start_ns,
            pool_acquire_end_ns=pool_acquire_end_ns,
            write_start_ns=write_start_ns,
            write_end_ns=write_end_ns,
            first_byte_ns=first_byte_ns,
            completion_ns=completion_ns,
            status_code=status_code,
            outcome=outcome,
            error_category=error_category,
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
            connection_reused=lease.reused if lease is not None else False,
        )
