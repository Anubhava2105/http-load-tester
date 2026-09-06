"""Versioned report data assembled from a test plan and metrics snapshot."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..domain.models import TestPlan
from .metrics import MetricsSnapshot


SCHEMA_VERSION = "1.0"


class ExitCode(IntEnum):
    SUCCESS = 0
    COMPLETED_WITH_ERRORS = 1
    EXECUTION_FAILURE = 2
    INVALID_CONFIGURATION = 3
    INTERRUPTED = 130


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _metric_mapping(mapping: Mapping[object, object]) -> dict[str, object]:
    return {str(_enum_value(key)): value for key, value in mapping.items()}


def _dataclass_mapping(value: object) -> dict[str, object]:
    return {item.name: getattr(value, item.name) for item in fields(value)}


def _target(plan: TestPlan) -> str:
    hostname = plan.origin.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    return f"{plan.origin.scheme}://{hostname}:{plan.origin.port}{plan.request.target}"


def _latency_mapping(mapping: Mapping[int, float | None]) -> dict[str, float | None]:
    return {str(percent): value for percent, value in mapping.items()}


@dataclass(frozen=True, slots=True)
class Report:
    """Immutable report payload with a stable JSON-compatible schema."""

    configuration: Mapping[str, object]
    metrics: MetricsSnapshot
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, MetricsSnapshot):
            raise TypeError("metrics must be a MetricsSnapshot")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("schema_version must be a non-empty string")
        object.__setattr__(
            self,
            "configuration",
            MappingProxyType(dict(self.configuration)),
        )

    @classmethod
    def from_plan(cls, plan: TestPlan, metrics: MetricsSnapshot) -> Report:
        if not isinstance(plan, TestPlan):
            raise TypeError("plan must be a TestPlan")
        return cls(
            configuration={
                "target": _target(plan),
                "method": plan.request.method,
                "request_target": plan.request.target,
                "request_count": plan.request_count,
                "duration_seconds": plan.duration_seconds,
                "warmup_seconds": plan.warmup_seconds,
                "workers": plan.workers,
                "max_connections": plan.max_connections,
                "load_model": plan.load_model.value,
                "target_rate": plan.target_rate,
                "request_body_bytes": len(plan.request.body),
                "request_header_count": len(plan.request.headers),
                "timeouts": _dataclass_mapping(plan.timeouts),
                "limits": _dataclass_mapping(plan.limits),
                "random_seed": plan.random_seed,
                "fault_mode": plan.fault_policy.mode.value if plan.fault_policy else None,
                "fault_probability": plan.fault_policy.probability if plan.fault_policy else None,
                "metrics_mode": plan.metrics.mode.value,
                "metrics_reservoir_size": plan.metrics.reservoir_size,
            },
            metrics=metrics,
        )

    def to_dict(self) -> dict[str, Any]:
        snapshot = self.metrics
        return {
            "schema_version": self.schema_version,
            "configuration": dict(self.configuration),
            "results": {
                "total_attempts": snapshot.total_attempts,
                "responses": snapshot.response_count,
                "successes": snapshot.success_count,
                "http_errors": snapshot.http_error_count,
                "errors": snapshot.failure_count,
                "error_rate": snapshot.error_rate,
                "throughput_requests_per_second": snapshot.throughput_requests_per_second,
                "bytes_sent": snapshot.bytes_sent,
                "bytes_received": snapshot.bytes_received,
                "connection_reuse_ratio": snapshot.connection_reuse_ratio,
                "status_codes": _metric_mapping(snapshot.status_counts),
                "outcomes": _metric_mapping(snapshot.outcome_counts),
                "error_categories": _metric_mapping(snapshot.error_counts),
                "transport_error_count": snapshot.transport_error_count,
                "protocol_error_count": snapshot.protocol_error_count,
                "timeout_count": snapshot.timeout_count,
                "cancelled_count": snapshot.cancelled_count,
                "pool_acquire_timeout_count": snapshot.pool_acquire_timeout_count,
                "metrics_mode": snapshot.metrics_mode,
                "approximate_percentiles": snapshot.approximate_percentiles,
                "metrics_reservoir_size": snapshot.reservoir_size,
                "latency_percentiles_ns": {
                    "request": _latency_mapping(snapshot.request_latency_percentiles_ns),
                    "end_to_end": _latency_mapping(
                        snapshot.end_to_end_latency_percentiles_ns
                    ),
                    "time_to_first_byte": _latency_mapping(
                        snapshot.time_to_first_byte_percentiles_ns
                    ),
                },
                "pool_wait_percentiles_ns": _latency_mapping(
                    snapshot.pool_wait_percentiles_ns
                ),
                "fault_applied_count": snapshot.fault_applied_count,
                "fault_mode_counts": dict(snapshot.fault_mode_counts),
            },
        }


def exit_code_for(
    report: Report,
    *,
    execution_failed: bool = False,
    configuration_failed: bool = False,
) -> ExitCode:
    if not isinstance(report, Report):
        raise TypeError("report must be a Report")
    if configuration_failed:
        return ExitCode.INVALID_CONFIGURATION
    if execution_failed:
        return ExitCode.EXECUTION_FAILURE
    if report.metrics.failure_count:
        return ExitCode.COMPLETED_WITH_ERRORS
    return ExitCode.SUCCESS
