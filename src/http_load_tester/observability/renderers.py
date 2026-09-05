"""Human-readable and JSON report renderers."""

from __future__ import annotations

import json
from typing import Iterable

from ..domain.errors import ErrorCategory
from .report import Report


def _milliseconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / 1_000_000:.3f} ms"


def _percentile_line(
    label: str,
    values: dict[int, float | None] | Iterable[tuple[int, float | None]],
) -> str:
    mapping = dict(values)
    return "\n".join(
        f"  p{percent:<3}       {_milliseconds(mapping.get(percent))}"
        for percent in (50, 95, 99)
        if percent in mapping
    ) or f"  {label:<12} n/a"


def _category_lines(
    title: str,
    categories: dict[object, int],
    *,
    include: set[ErrorCategory] | None = None,
) -> list[str]:
    entries = [
        (getattr(category, "value", str(category)), count)
        for category, count in categories.items()
        if count and (include is None or category in include)
    ]
    if not entries:
        return []
    return [f"{title}:"] + [f"  {name:<22} {count}" for name, count in sorted(entries)]


class TerminalRenderer:
    """Render a report for a human reading a terminal."""

    def render(self, report: Report) -> str:
        if not isinstance(report, Report):
            raise TypeError("report must be a Report")
        configuration = report.configuration
        snapshot = report.metrics
        lines = [
            f"Target:       {configuration['target']}",
            f"Mode:         {configuration['load_model']}",
            f"Workers:      {configuration['workers']}",
            f"Connections:  {configuration['max_connections']}",
        ]
        if configuration["request_count"] is not None:
            lines.append(f"Request count: {configuration['request_count']}")
        if configuration["duration_seconds"] is not None:
            lines.append(f"Duration:     {configuration['duration_seconds']}s")
        if configuration["target_rate"] is not None:
            lines.append(f"Target rate:  {configuration['target_rate']} req/s")
        lines.extend(
            [
                "",
                f"Attempts:     {snapshot.total_attempts}",
                f"Responses:    {snapshot.response_count}",
                f"Errors:       {snapshot.failure_count} ({snapshot.error_rate:.2%})",
                (
                    "Throughput:   "
                    f"{snapshot.throughput_requests_per_second:.3f} req/s"
                    if snapshot.throughput_requests_per_second is not None
                    else "Throughput:   n/a"
                ),
                (
                    "Reuse ratio:  "
                    f"{snapshot.connection_reuse_ratio:.2%}"
                    if snapshot.connection_reuse_ratio is not None
                    else "Reuse ratio:  n/a"
                ),
                "",
                "Latency:",
                _percentile_line("latency", snapshot.request_latency_percentiles_ns),
                "",
                "End-to-end latency:",
                _percentile_line(
                    "end-to-end", snapshot.end_to_end_latency_percentiles_ns
                ),
                "",
                "Pool wait:",
                _percentile_line("pool wait", snapshot.pool_wait_percentiles_ns),
            ]
        )
        if snapshot.status_counts:
            lines.extend(["", "Status codes:"])
            lines.extend(
                f"  {status:<22} {count}"
                for status, count in sorted(snapshot.status_counts.items())
            )
        lines.extend(
            _category_lines(
                "Transport errors",
                snapshot.error_counts,
                include={
                    ErrorCategory.DNS_FAILURE,
                    ErrorCategory.CONNECT_TIMEOUT,
                    ErrorCategory.CONNECTION_REFUSED,
                    ErrorCategory.TRANSPORT_FAILURE,
                    ErrorCategory.TLS_FAILURE,
                    ErrorCategory.WRITE_TIMEOUT,
                    ErrorCategory.READ_TIMEOUT,
                    ErrorCategory.CONNECTION_RESET,
                    ErrorCategory.CONNECTION_CLOSED_EARLY,
                },
            )
        )
        lines.extend(
            _category_lines(
                "Protocol errors",
                snapshot.error_counts,
                include={
                    ErrorCategory.PROTOCOL_ERROR,
                    ErrorCategory.INVALID_HEADERS,
                    ErrorCategory.INVALID_BODY_FRAMING,
                    ErrorCategory.RESPONSE_TOO_LARGE,
                },
            )
        )
        lines.extend(
            _category_lines(
                "Pool acquisition errors",
                snapshot.error_counts,
                include={ErrorCategory.POOL_ACQUIRE_TIMEOUT},
            )
        )
        return "\n".join(lines) + "\n"


class JsonRenderer:
    """Render a report as stable, versioned JSON."""

    def __init__(self, *, indent: int | None = 2) -> None:
        if indent is not None and (
            isinstance(indent, bool) or not isinstance(indent, int) or indent < 0
        ):
            raise ValueError("indent must be a non-negative integer or None")
        self._indent = indent

    def render(self, report: Report) -> str:
        if not isinstance(report, Report):
            raise TypeError("report must be a Report")
        return json.dumps(
            report.to_dict(),
            indent=self._indent,
            sort_keys=True,
        ) + "\n"


def render_terminal(report: Report) -> str:
    return TerminalRenderer().render(report)


def render_json(report: Report, *, indent: int | None = 2) -> str:
    return JsonRenderer(indent=indent).render(report)
