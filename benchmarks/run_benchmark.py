"""Reproducible benchmark harness for the blocking HTTP load tester.

Runs a warmup phase (discarded) followed by one measured phase against
either the local deterministic scenario server or an explicitly supplied
target URL, then prints one machine-readable JSON document carrying the
workload, the environment, and the measured results.

Correctness checks live in ``tests/``; this harness only measures.
Every number it prints was measured in the run it describes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import time
import tracemalloc
from collections.abc import Sequence

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None  # type: ignore[assignment]

from http_load_tester.application.runner import create_pool
from http_load_tester.domain.errors import ConfigurationError
from http_load_tester.domain.models import (
    HttpRequest,
    LoadModel,
    MetricsConfig,
    MetricsMode,
    Origin,
    TestPlan,
    TimeoutConfig,
)
from http_load_tester.http.url import parse_target
from http_load_tester.load.executor import WorkExecutor
from http_load_tester.observability.metrics import MetricsCollector
from http_load_tester.observability.report import ExitCode
from test_server import Scenario, ScenarioConfig, ScenarioServer


BENCHMARK_VERSION = "1.0"

LOCAL_SCENARIOS = ("fixed", "chunked", "large_response")

RESULT_NOTE = (
    "Machine- and workload-specific. Numbers describe only the run above; "
    "do not compare across machines, targets, or workloads unless the "
    "environment and workload are controlled and recorded."
)


class _BenchmarkParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConfigurationError(message)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative and finite")
    return parsed


def create_parser() -> argparse.ArgumentParser:
    parser = _BenchmarkParser(
        prog="run_benchmark",
        description="Measure one reproducible load-test workload and emit JSON.",
    )
    parser.add_argument(
        "--target-url",
        default=None,
        help="explicit external target URL; when omitted, the local scenario "
        "server is used and no external network is touched",
    )
    parser.add_argument(
        "--scenario",
        choices=LOCAL_SCENARIOS,
        default="fixed",
        help="local scenario-server response shape (ignored with --target-url)",
    )
    parser.add_argument("--label", default="local-benchmark")
    parser.add_argument("-X", "--method", default="GET", help="HTTP method")
    parser.add_argument("--path", default="/", help="request path")
    parser.add_argument("--body", default="", help="UTF-8 request body")
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument(
        "-n", "--count", dest="request_count", type=_positive_int,
        help="measured-phase request count",
    )
    workload.add_argument(
        "--duration", dest="duration_seconds", type=_positive_float,
        help="measured-phase duration in seconds (default: 5)",
    )
    parser.add_argument(
        "--warmup", type=_non_negative_float, default=1.0,
        help="warmup seconds, excluded from measurement (default: 1)",
    )
    parser.add_argument(
        "--load-model",
        choices=tuple(model.value for model in LoadModel),
        default=LoadModel.CLOSED_LOOP.value,
        dest="load_model",
    )
    parser.add_argument("--rate", dest="target_rate", type=_positive_float)
    parser.add_argument("--workers", type=_positive_int, default=2)
    parser.add_argument("--max-connections", type=_positive_int, default=2)
    parser.add_argument("--request-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--pool-timeout", type=_positive_float, default=30.0)
    parser.add_argument(
        "--metrics-mode",
        choices=tuple(mode.value for mode in MetricsMode),
        default=MetricsMode.EXACT.value,
        dest="metrics_mode",
    )
    parser.add_argument(
        "--metrics-reservoir-size",
        type=_positive_int,
        default=1024,
        dest="metrics_reservoir_size",
    )
    parser.add_argument("--output", default=None, help="write the JSON document to this file too")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return create_parser().parse_args(argv)


def collect_environment() -> dict:
    """Describe the machine behind the numbers. No network involved."""
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def _percentile_triplet(mapping: dict) -> dict:
    return {
        "p50": mapping.get(50),
        "p95": mapping.get(95),
        "p99": mapping.get(99),
    }


def _peak_rss_bytes() -> int | None:
    """Peak resident memory in bytes, or None where unavailable."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(peak)
    return int(peak) * 1024


def _build_plan(
    args: argparse.Namespace,
    origin: Origin,
    *,
    request_count: int | None,
    duration_seconds: float | None,
) -> TestPlan:
    return TestPlan(
        origin=origin,
        request=HttpRequest(
            method=args.method,
            target=args.path,
            body=args.body.encode("utf-8"),
        ),
        request_count=request_count,
        duration_seconds=duration_seconds,
        workers=args.workers,
        max_connections=args.max_connections,
        load_model=LoadModel(args.load_model),
        target_rate=args.target_rate,
        timeouts=TimeoutConfig(
            request_seconds=args.request_timeout,
            pool_acquire_seconds=args.pool_timeout,
        ),
        metrics=MetricsConfig(
            mode=MetricsMode(args.metrics_mode),
            reservoir_size=args.metrics_reservoir_size,
        ),
    )


def _run_measured(plan: TestPlan) -> tuple:
    """Run one plan phase and return its metrics snapshot plus elapsed ns."""
    pool = create_pool(plan)
    executor = WorkExecutor(plan, pool)
    tracemalloc.start()
    started_ns = time.perf_counter_ns()
    try:
        samples = executor.run()
    finally:
        elapsed_ns = time.perf_counter_ns() - started_ns
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    collector = MetricsCollector(
        mode=plan.metrics.mode,
        reservoir_size=plan.metrics.reservoir_size,
    )
    collector.extend(samples)
    return collector.snapshot(run_duration_ns=elapsed_ns), traced_peak


def run_benchmark(args: argparse.Namespace) -> dict:
    """Run warmup plus one measured phase; return a JSON-ready document."""
    if not isinstance(args, argparse.Namespace):
        raise TypeError("args must be a Namespace")
    duration = args.duration_seconds if args.request_count is None else None
    if args.request_count is None and duration is None:
        duration = 5.0

    server: ScenarioServer | None = None
    try:
        if args.target_url is not None:
            parsed = parse_target(args.target_url)
            origin = parsed.origin
            target_url = args.target_url
            target_kind = "external"
            scenario_name: str | None = None
        else:
            scenario = Scenario(args.scenario)
            server = ScenarioServer(ScenarioConfig(scenario=scenario))
            server.start()
            host, port = server.address
            origin = Origin("http", host, port)
            target_url = server.url
            target_kind = "local"
            scenario_name = scenario.value

        if args.warmup > 0:
            warmup_plan = _build_plan(
                args, origin, request_count=None, duration_seconds=args.warmup
            )
            _run_measured(warmup_plan)

        plan = _build_plan(
            args, origin, request_count=args.request_count, duration_seconds=duration
        )
        snapshot, traced_peak = _run_measured(plan)
    finally:
        if server is not None:
            server.close()

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "label": args.label,
        "note": RESULT_NOTE,
        "environment": collect_environment(),
        "workload": {
            "target_kind": target_kind,
            "target_url": target_url,
            "scenario": scenario_name,
            "method": plan.request.method,
            "path": plan.request.target,
            "request_body_bytes": len(plan.request.body),
            "workers": plan.workers,
            "max_connections": plan.max_connections,
            "warmup_seconds": args.warmup,
            "request_count": plan.request_count,
            "duration_seconds": plan.duration_seconds,
            "load_model": plan.load_model.value,
            "target_rate": plan.target_rate,
            "metrics_mode": plan.metrics.mode.value,
            "metrics_reservoir_size": plan.metrics.reservoir_size,
            "request_timeout_seconds": plan.timeouts.request_seconds,
            "pool_timeout_seconds": plan.timeouts.pool_acquire_seconds,
        },
        "results": {
            "total_attempts": snapshot.total_attempts,
            "successes": snapshot.success_count,
            "http_errors": snapshot.http_error_count,
            "errors": snapshot.failure_count,
            "error_rate": snapshot.error_rate,
            "throughput_requests_per_second": snapshot.throughput_requests_per_second,
            "request_latency_ns": _percentile_triplet(
                dict(snapshot.request_latency_percentiles_ns)
            ),
            "end_to_end_latency_ns": _percentile_triplet(
                dict(snapshot.end_to_end_latency_percentiles_ns)
            ),
            "pool_wait_ns": _percentile_triplet(
                dict(snapshot.pool_wait_percentiles_ns)
            ),
            "time_to_first_byte_ns": _percentile_triplet(
                dict(snapshot.time_to_first_byte_percentiles_ns)
            ),
            "connection_reuse_ratio": snapshot.connection_reuse_ratio,
            "bytes_sent": snapshot.bytes_sent,
            "bytes_received": snapshot.bytes_received,
            "approximate_percentiles": snapshot.approximate_percentiles,
        },
        "memory": {
            "peak_rss_bytes": _peak_rss_bytes(),
            "tracemalloc_peak_bytes": traced_peak,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = run_benchmark(args)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return int(ExitCode.INVALID_CONFIGURATION)
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTION_FAILURE)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output is not None:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    return int(ExitCode.SUCCESS)


if __name__ == "__main__":
    raise SystemExit(main())
