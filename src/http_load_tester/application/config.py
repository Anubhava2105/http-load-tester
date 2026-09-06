"""Translate command-line arguments into a validated TestPlan."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence

from ..domain.errors import ConfigurationError
from ..domain.models import FaultMode, FaultPolicy, HttpRequest, LoadModel, ReportFormat, TestPlan, TimeoutConfig
from ..http.url import parse_target


class _ArgumentParser(argparse.ArgumentParser):
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

def _header(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("headers must use 'Name: value' syntax")
    name, header_value = value.split(":", 1)
    name = name.strip()
    header_value = header_value.strip()
    if not name:
        raise argparse.ArgumentTypeError("header name must not be empty")
    return name, header_value


def create_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="http-load-tester",
        description="Run a bounded raw HTTP/1.1 closed- or open-loop load test.",
    )
    parser.add_argument("url", help="HTTP or HTTPS target URL")
    parser.add_argument("-X", "--method", default="GET", help="HTTP method")
    parser.add_argument(
        "--header",
        action="append",
        type=_header,
        default=[],
        metavar="NAME: VALUE",
        help="request header; may be repeated",
    )
    parser.add_argument("--body", default="", help="UTF-8 request body")
    workload = parser.add_mutually_exclusive_group(required=True)
    workload.add_argument(
        "-n",
        "--count",
        dest="request_count",
        type=_positive_int,
        help="number of requests",
    )
    workload.add_argument(
        "--duration",
        dest="duration_seconds",
        type=_positive_float,
        help="test duration in seconds",
    )
    parser.add_argument("--warmup", type=_non_negative_float, default=0.0)
    parser.add_argument(
        "--load-model",
        "--mode",
        choices=tuple(model.value for model in LoadModel),
        default=LoadModel.CLOSED_LOOP.value,
        dest="load_model",
    )
    parser.add_argument("--rate", dest="target_rate", type=_positive_float)
    parser.add_argument("--workers", type=_positive_int, default=1)
    parser.add_argument("--max-connections", type=_positive_int, default=1)
    parser.add_argument("--request-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--pool-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--server-name", help="TLS SNI/server-name override")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification",
    )
    parser.add_argument(
        "--format",
        choices=(ReportFormat.TERMINAL.value, ReportFormat.JSON.value),
        default=ReportFormat.TERMINAL.value,
        dest="report_format",
    )
    parser.add_argument(
        "--max-runtime",
        type=_positive_float,
        default=None,
        dest="max_runtime",
        help="overall runtime ceiling in seconds",
    )
    fault_group = parser.add_argument_group("fault injection")
    fault_group.add_argument(
        "--fault-mode",
        choices=tuple(mode.value for mode in FaultMode),
        default=FaultMode.NONE.value,
        dest="fault_mode",
        help="client-side fault-injection mode (default: none)",
    )
    fault_group.add_argument(
        "--fault-probability",
        type=_non_negative_float,
        default=1.0,
        dest="fault_probability",
        help="probability of applying the fault per attempt (0.0-1.0)",
    )
    fault_group.add_argument(
        "--fault-delay",
        type=_non_negative_float,
        default=0.1,
        dest="fault_delay",
        help="delay in seconds for slow-body or short-timeout modes",
    )
    fault_group.add_argument(
        "--fault-body-bytes",
        type=_positive_int,
        default=None,
        dest="fault_body_bytes",
        help="body size in bytes for randomized-body mode",
    )
    fault_group.add_argument(
        "--fault-seed",
        type=int,
        default=0,
        dest="fault_seed",
        help="random seed for deterministic fault decisions",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return create_parser().parse_args(argv)


def plan_from_args(args: argparse.Namespace) -> TestPlan:
    parsed = parse_target(
        args.url,
        tls_verify=not args.insecure,
        server_name=args.server_name,
    )
    request = HttpRequest(
        method=args.method,
        target=parsed.request_target,
        headers=args.header,
        body=args.body.encode("utf-8"),
    )
    timeouts = TimeoutConfig(
        request_seconds=args.request_timeout,
        pool_acquire_seconds=args.pool_timeout,
    )
    fault_mode = FaultMode(args.fault_mode)
    fault_policy = None
    if fault_mode is not FaultMode.NONE:
        fault_policy = FaultPolicy(
            mode=fault_mode,
            probability=args.fault_probability,
            delay_seconds=args.fault_delay,
            randomized_body_bytes=args.fault_body_bytes,
            seed=args.fault_seed,
        )
    from ..domain.models import SafetyLimits
    limits_kwargs = {}
    if args.max_runtime is not None:
        limits_kwargs["max_total_runtime_seconds"] = args.max_runtime
    limits = SafetyLimits(**limits_kwargs)
    return TestPlan(
        origin=parsed.origin,
        target_rate=args.target_rate,
        request=request,
        request_count=args.request_count,
        duration_seconds=args.duration_seconds,
        warmup_seconds=args.warmup,
        workers=args.workers,
        max_connections=args.max_connections,
        load_model=LoadModel(args.load_model),
        timeouts=timeouts,
        limits=limits,
        report_format=ReportFormat(args.report_format),
        fault_policy=fault_policy,
    )


def load_plan(argv: Sequence[str] | None = None) -> TestPlan:
    return plan_from_args(parse_args(argv))
