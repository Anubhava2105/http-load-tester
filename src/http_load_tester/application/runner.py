"""Application composition root for a single-origin load test."""

from __future__ import annotations

from collections.abc import Callable
import sys
import time
from typing import TextIO

from ..domain.errors import ConfigurationError
from ..domain.models import ReportFormat, TestPlan
from ..load.executor import WorkExecutor
from ..observability.metrics import MetricsCollector
from ..observability.renderers import render_json, render_terminal
from ..observability.report import ExitCode, Report, exit_code_for
from ..observability.samples import SampleCollector
from ..pool.connection_pool import ConnectionPool


PoolFactory = Callable[[TestPlan], ConnectionPool]
ExecutorFactory = Callable[..., WorkExecutor]


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
    metrics = MetricsCollector()
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
    executor = executor_factory(
        plan,
        pool,
        sample_sink=samples_collector.submit,
    )

    runtime_deadline_ns = time.perf_counter_ns() + int(
        plan.limits.max_total_runtime_seconds * 1_000_000_000
    )

    started_ns = time.perf_counter_ns()
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
        samples_collector.close()

    collected = samples_collector.collect()
    report = _build_report(plan, collected, started_ns)
    rendered = _render_report(plan, report)
    output.write(rendered)
    output.flush()

    if interrupted:
        return int(ExitCode.INTERRUPTED)
    return int(exit_code_for(report))
