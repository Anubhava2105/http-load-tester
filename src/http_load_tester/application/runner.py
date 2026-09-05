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
    if plan.load_model.value != "closed_loop":
        raise ConfigurationError("the CLI currently supports closed-loop plans only")
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    samples = SampleCollector()
    metrics = MetricsCollector()
    pool = pool_factory(plan)
    executor = executor_factory(
        plan,
        pool,
        sample_sink=samples.submit,
    )
    started_ns = time.perf_counter_ns()
    try:
        executor.run()
    except KeyboardInterrupt:
        executor.cancel()
        print("load test cancelled", file=errors)
        return int(ExitCode.EXECUTION_FAILURE)
    except Exception as exc:
        print(f"load test failed: {exc}", file=errors)
        return int(ExitCode.EXECUTION_FAILURE)
    finally:
        samples.close()
    collected = samples.collect()
    metrics.extend(collected)
    duration_ns = max(0, time.perf_counter_ns() - started_ns)
    report = Report.from_plan(
        plan,
        metrics.snapshot(run_duration_ns=duration_ns),
    )
    rendered = (
        render_json(report)
        if plan.report_format is ReportFormat.JSON
        else render_terminal(report)
    )
    output.write(rendered)
    output.flush()
    return int(exit_code_for(report))
