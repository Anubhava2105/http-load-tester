"""Application composition root for a single-origin load test."""

from __future__ import annotations

from collections.abc import Callable
import sys
import threading
import time
from typing import TextIO

from ..domain.errors import ConfigurationError
from ..domain.models import ReportFormat, ResultSample, TestPlan
from ..load.executor import WorkExecutor
from ..observability.metrics import MetricsCollector
from ..observability.renderers import render_json, render_terminal
from ..observability.report import ExitCode, Report, exit_code_for
from ..observability.samples import SampleCollector
from ..pool.connection_pool import ConnectionPool


PoolFactory = Callable[[TestPlan], ConnectionPool]
ExecutorFactory = Callable[..., WorkExecutor]

PROGRESS_INTERVAL_SECONDS = 2.0


class ProgressTracker:
    """Count completed attempts and format live progress lines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed = 0

    def record(self, sample: ResultSample) -> None:
        with self._lock:
            self._completed += 1

    @property
    def completed(self) -> int:
        with self._lock:
            return self._completed

    def line(self, elapsed_seconds: float, total: int | None) -> str:
        if total is not None:
            return f"[{elapsed_seconds:.0f}s] {self.completed}/{total} attempts"
        return f"[{elapsed_seconds:.0f}s] {self.completed} attempts"


def create_pool(plan: TestPlan) -> ConnectionPool:
    return ConnectionPool(
        plan.origin,
        plan.max_connections,
        plan.timeouts,
        plan.limits,
    )


def validate_plan_limits(plan: TestPlan) -> None:
    """Fail fast if the plan exceeds safety limits before opening sockets."""
    limits = plan.limits
    if plan.workers > limits.max_workers:
        raise ConfigurationError("workers exceeds max_workers")
    if plan.max_connections > limits.max_connections:
        raise ConfigurationError("max_connections exceeds max_connections limit")
    if plan.request_count is not None and plan.request_count > limits.max_requests:
        raise ConfigurationError("request_count exceeds max_requests")
    if plan.duration_seconds is not None and plan.duration_seconds > limits.max_duration_seconds:
        raise ConfigurationError("duration_seconds exceeds max_duration_seconds")
    if len(plan.request.body) > limits.max_request_body_bytes:
        raise ConfigurationError("request body exceeds max_request_body_bytes")
    total_planned = plan.warmup_seconds + (plan.duration_seconds or 0)
    if total_planned > limits.max_total_runtime_seconds:
        raise ConfigurationError("warmup + duration exceeds max_total_runtime_seconds")


def _build_report(plan: TestPlan, samples: tuple, started_ns: int) -> Report:
    metrics = MetricsCollector(
        mode=plan.metrics.mode,
        reservoir_size=plan.metrics.reservoir_size,
    )
    metrics.extend(samples)
    duration_ns = max(0, time.perf_counter_ns() - started_ns)
    return Report.from_plan(plan, metrics.snapshot(run_duration_ns=duration_ns))


def _render_report(plan: TestPlan, report: Report) -> str:
    if plan.report_format is ReportFormat.JSON:
        return render_json(report)
    return render_terminal(report)


def run_plan(
    plan: TestPlan,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    pool_factory: PoolFactory = create_pool,
    executor_factory: ExecutorFactory = WorkExecutor,
) -> int:
    """Run one plan, render its report, and return a stable exit code."""
    if not isinstance(plan, TestPlan):
        raise TypeError("plan must be a TestPlan")

    output = stdout or sys.stdout
    errors = stderr or sys.stderr

    try:
        validate_plan_limits(plan)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=errors)
        return int(ExitCode.INVALID_CONFIGURATION)

    samples_collector = SampleCollector()
    pool = pool_factory(plan)
    progress = ProgressTracker()

    def _counting_sink(sample: ResultSample) -> None:
        progress.record(sample)
        samples_collector.submit(sample)

    executor = executor_factory(
        plan,
        pool,
        sample_sink=_counting_sink,
    )

    runtime_deadline_ns = time.perf_counter_ns() + int(
        plan.limits.max_total_runtime_seconds * 1_000_000_000
    )

    started_ns = time.perf_counter_ns()
    progress_stop = threading.Event()
    progress_thread = _maybe_start_progress(
        progress, progress_stop, started_ns, plan, errors
    )
    interrupted = False
    try:
        executor.run(runtime_deadline_ns=runtime_deadline_ns)
    except KeyboardInterrupt:
        executor.cancel()
        interrupted = True
    except Exception as exc:
        print(f"load test failed: {exc}", file=errors)
        return int(ExitCode.EXECUTION_FAILURE)
    finally:
        progress_stop.set()
        if progress_thread is not None:
            progress_thread.join(timeout=5)
        samples_collector.close()

    collected = samples_collector.collect()
    report = _build_report(plan, collected, started_ns)
    rendered = _render_report(plan, report)
    output.write(rendered)
    output.flush()
    if plan.output_path is not None:
        try:
            with open(plan.output_path, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            print(f"failed to write report: {exc}", file=errors)
            return int(ExitCode.EXECUTION_FAILURE)

    if interrupted:
        return int(ExitCode.INTERRUPTED)
    return int(exit_code_for(report))


def _maybe_start_progress(
    progress: ProgressTracker,
    stop: threading.Event,
    started_ns: int,
    plan: TestPlan,
    errors: TextIO,
) -> threading.Thread | None:
    """Print live progress to interactive terminals, nothing otherwise."""
    isatty = getattr(errors, "isatty", None)
    if not callable(isatty) or not isatty():
        return None
    total = plan.request_count

    def _loop() -> None:
        while not stop.wait(PROGRESS_INTERVAL_SECONDS):
            elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000_000
            errors.write(progress.line(elapsed, total) + "\n")
            errors.flush()

    thread = threading.Thread(target=_loop, name="http-load-progress", daemon=True)
    thread.start()
    return thread
