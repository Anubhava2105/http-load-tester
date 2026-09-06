"""Immutable domain models shared by the HTTP load tester subsystems."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from numbers import Real
import math
import re
from typing import Iterable, Mapping

from .errors import ConfigurationError, ErrorCategory

Headers = tuple[tuple[str, str], ...]

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LoadModel(StrEnum):
    CLOSED_LOOP = "closed_loop"
    OPEN_LOOP = "open_loop"


class ReportFormat(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"


class MetricsMode(StrEnum):
    EXACT = "exact"
    BOUNDED = "bounded"


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    mode: MetricsMode = MetricsMode.EXACT
    reservoir_size: int = 1024

    def __post_init__(self) -> None:
        if not isinstance(self.mode, MetricsMode):
            raise ConfigurationError("metrics mode must be a MetricsMode")
        reservoir = _require_positive_int(self.reservoir_size, "reservoir_size")
        if reservoir > 1_000_000:
            raise ConfigurationError("reservoir_size must not exceed 1_000_000")
        object.__setattr__(self, "reservoir_size", reservoir)


class Outcome(StrEnum):
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    PROTOCOL_ERROR = "protocol_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{field_name} must be a non-empty string")
    if _CONTROL_RE.search(value):
        raise ConfigurationError(f"{field_name} must not contain control characters")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"{field_name} must be a non-negative integer")
    return value


def _require_positive_real(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ConfigurationError(f"{field_name} must be positive")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ConfigurationError(f"{field_name} must be positive and finite")
    return numeric


def _require_non_negative_real(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ConfigurationError(f"{field_name} must be non-negative")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ConfigurationError(f"{field_name} must be non-negative and finite")
    return numeric


def _normalize_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> Headers:
    items = headers.items() if isinstance(headers, Mapping) else headers
    normalized: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ConfigurationError("headers must contain (name, value) pairs")
        name, value = item
        if not isinstance(name, str) or not _TOKEN_RE.fullmatch(name):
            raise ConfigurationError(f"invalid header name: {name!r}")
        if not isinstance(value, str) or _CONTROL_RE.search(value):
            raise ConfigurationError(f"invalid value for header {name!r}")
        normalized.append((name, value))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class Origin:
    scheme: str
    hostname: str
    port: int
    tls_verify: bool = True
    server_name: str | None = None

    def __post_init__(self) -> None:
        scheme = _require_text(self.scheme, "scheme").lower()
        if scheme not in {"http", "https"}:
            raise ConfigurationError("scheme must be http or https")
        hostname = _require_text(self.hostname, "hostname")
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]
        if any(character.isspace() for character in hostname) or "/" in hostname:
            raise ConfigurationError("hostname must not contain whitespace or '/'")
        if not hostname:
            raise ConfigurationError("hostname must be a non-empty string")
        port = _require_positive_int(self.port, "port")
        if port > 65535:
            raise ConfigurationError("port must be between 1 and 65535")
        if not isinstance(self.tls_verify, bool):
            raise ConfigurationError("tls_verify must be a boolean")
        server_name = self.server_name
        if server_name is not None:
            server_name = _require_text(server_name, "server_name")
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "hostname", hostname)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "server_name", server_name)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    target: str
    headers: Headers = field(default_factory=tuple)
    body: bytes = b""
    request_id: str = "request-1"

    def __post_init__(self) -> None:
        method = _require_text(self.method, "method").upper()
        if not _TOKEN_RE.fullmatch(method):
            raise ConfigurationError("method must be an HTTP token")
        target = _require_text(self.target, "target")
        if target != "*" and not target.startswith("/"):
            raise ConfigurationError("target must use origin-form or '*'")
        if any(character.isspace() for character in target):
            raise ConfigurationError("target must not contain whitespace")
        if not isinstance(self.body, bytes):
            raise ConfigurationError("body must be bytes")
        request_id = _require_text(self.request_id, "request_id")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "headers", _normalize_headers(self.headers))
        object.__setattr__(self, "request_id", request_id)


@dataclass(frozen=True, slots=True)
class HttpResponseSummary:
    http_version: str
    status_code: int
    reason: str = ""
    headers: Headers = field(default_factory=tuple)
    body_bytes: int = 0
    body_complete: bool = True
    connection_reusable: bool = False

    def __post_init__(self) -> None:
        version = _require_text(self.http_version, "http_version")
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ConfigurationError("http_version must be HTTP/1.0 or HTTP/1.1")
        status_code = _require_positive_int(self.status_code, "status_code")
        if status_code < 100 or status_code > 599:
            raise ConfigurationError("status_code must be between 100 and 599")
        reason = self.reason
        if not isinstance(reason, str) or _CONTROL_RE.search(reason):
            raise ConfigurationError("reason must not contain control characters")
        body_bytes = _require_non_negative_int(self.body_bytes, "body_bytes")
        if not isinstance(self.body_complete, bool):
            raise ConfigurationError("body_complete must be a boolean")
        if not isinstance(self.connection_reusable, bool):
            raise ConfigurationError("connection_reusable must be a boolean")
        if not self.body_complete and self.connection_reusable:
            raise ConfigurationError("an incomplete response cannot be reusable")
        object.__setattr__(self, "http_version", version)
        object.__setattr__(self, "headers", _normalize_headers(self.headers))
        object.__setattr__(self, "body_bytes", body_bytes)


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    dns_seconds: float = 5.0
    connect_seconds: float = 5.0
    tls_handshake_seconds: float = 5.0
    write_seconds: float = 5.0
    read_seconds: float = 5.0
    request_seconds: float = 30.0
    pool_acquire_seconds: float = 30.0

    def __post_init__(self) -> None:
        for field_name in (
            "dns_seconds",
            "connect_seconds",
            "tls_handshake_seconds",
            "write_seconds",
            "read_seconds",
            "request_seconds",
            "pool_acquire_seconds",
        ):
            value = _require_positive_real(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    max_workers: int = 100
    max_connections: int = 100
    max_requests: int = 1_000_000
    max_duration_seconds: float = 86_400.0
    max_request_body_bytes: int = 10 * 1024 * 1024
    max_response_body_bytes: int = 100 * 1024 * 1024
    max_header_bytes: int = 64 * 1024
    max_header_count: int = 100
    max_total_runtime_seconds: float = 86_400.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_workers",
            "max_connections",
            "max_requests",
            "max_header_bytes",
            "max_header_count",
        ):
            value = _require_positive_int(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        for field_name in (
            "max_duration_seconds",
            "max_total_runtime_seconds",
        ):
            value = _require_positive_real(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        for field_name in (
            "max_request_body_bytes",
            "max_response_body_bytes",
        ):
            value = _require_non_negative_int(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)


class FaultMode(StrEnum):
    NONE = "none"
    RAMP_UP = "ramp_up"
    TRAFFIC_SPIKE = "traffic_spike"
    CONNECTION_CHURN = "connection_churn"
    SLOW_REQUEST_BODY = "slow_request_body"
    ABORT_AFTER_HEADERS = "abort_after_headers"
    ABORT_DURING_RESPONSE = "abort_during_response"
    SHORT_READ_TIMEOUT = "short_read_timeout"
    RANDOMIZED_BODY = "randomized_body"


@dataclass(frozen=True, slots=True)
class FaultPolicy:
    mode: FaultMode = FaultMode.NONE
    probability: float = 1.0
    delay_seconds: float = 0.1
    randomized_body_bytes: int | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FaultMode):
            raise ConfigurationError("fault mode must be a FaultMode")
        if isinstance(self.probability, bool) or not isinstance(self.probability, (int, float)):
            raise ConfigurationError("fault probability must be numeric")
        if not 0 <= float(self.probability) <= 1:
            raise ConfigurationError("fault probability must be between 0 and 1")
        if isinstance(self.delay_seconds, bool) or not isinstance(
            self.delay_seconds, (int, float)
        ) or self.delay_seconds < 0:
            raise ConfigurationError("fault delay_seconds must be non-negative")
        if self.randomized_body_bytes is not None and (
            isinstance(self.randomized_body_bytes, bool)
            or not isinstance(self.randomized_body_bytes, int)
            or self.randomized_body_bytes < 0
        ):
            raise ConfigurationError("fault randomized_body_bytes must be non-negative or None")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ConfigurationError("fault seed must be an integer")


@dataclass(frozen=True, slots=True)
class FaultDecision:
    request: "HttpRequest"
    delay_seconds: float = 0.0
    force_connection_close: bool = False
    abort_after_headers: bool = False
    abort_during_response: bool = False
    read_timeout_seconds: float | None = None
    applied: bool = False
    fault_mode: str | None = None


@dataclass(frozen=True, slots=True)
class TestPlan:
    origin: Origin
    request: HttpRequest
    request_count: int | None = None
    duration_seconds: float | None = None
    warmup_seconds: float = 0.0
    workers: int = 1
    max_connections: int = 1
    load_model: LoadModel = LoadModel.CLOSED_LOOP
    target_rate: float | None = None
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: SafetyLimits = field(default_factory=SafetyLimits)
    report_format: ReportFormat = ReportFormat.TERMINAL
    random_seed: int = 0
    fault_policy: FaultPolicy | None = None
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.origin, Origin):
            raise ConfigurationError("origin must be an Origin")
        if not isinstance(self.request, HttpRequest):
            raise ConfigurationError("request must be an HttpRequest")
        if not isinstance(self.timeouts, TimeoutConfig):
            raise ConfigurationError("timeouts must be a TimeoutConfig")
        if not isinstance(self.limits, SafetyLimits):
            raise ConfigurationError("limits must be a SafetyLimits")
        if not isinstance(self.load_model, LoadModel):
            raise ConfigurationError("load_model must be a LoadModel")
        if not isinstance(self.report_format, ReportFormat):
            raise ConfigurationError("report_format must be a ReportFormat")
        if (self.request_count is None) == (self.duration_seconds is None):
            raise ConfigurationError("set exactly one of request_count or duration_seconds")
        if self.request_count is not None:
            request_count = _require_positive_int(self.request_count, "request_count")
            if request_count > self.limits.max_requests:
                raise ConfigurationError("request_count exceeds max_requests")
            object.__setattr__(self, "request_count", request_count)
        if self.duration_seconds is not None:
            duration = _require_positive_real(self.duration_seconds, "duration_seconds")
            if duration > self.limits.max_duration_seconds:
                raise ConfigurationError("duration_seconds exceeds max_duration_seconds")
            object.__setattr__(self, "duration_seconds", duration)
        warmup = _require_non_negative_real(self.warmup_seconds, "warmup_seconds")
        total_planned = warmup + (self.duration_seconds or 0)
        if total_planned > self.limits.max_total_runtime_seconds:
            raise ConfigurationError(
                "warmup + duration exceeds max_total_runtime_seconds"
            )
        workers = _require_positive_int(self.workers, "workers")
        max_connections = _require_positive_int(self.max_connections, "max_connections")
        if workers > self.limits.max_workers:
            raise ConfigurationError("workers exceeds max_workers")
        if max_connections > self.limits.max_connections:
            raise ConfigurationError("max_connections exceeds max_connections limit")
        if len(self.request.body) > self.limits.max_request_body_bytes:
            raise ConfigurationError("request body exceeds max_request_body_bytes")
        if not isinstance(self.load_model, LoadModel):
            raise ConfigurationError("load_model must be a LoadModel")
        if self.load_model is LoadModel.OPEN_LOOP:
            if self.target_rate is None:
                raise ConfigurationError("open_loop requires a positive target_rate")
            target_rate = _require_positive_real(self.target_rate, "target_rate")
        elif self.target_rate is not None:
            raise ConfigurationError("target_rate is only valid for open_loop")
        else:
            target_rate = None
        if not isinstance(self.timeouts, TimeoutConfig):
            raise ConfigurationError("timeouts must be a TimeoutConfig")
        if not isinstance(self.limits, SafetyLimits):
            raise ConfigurationError("limits must be a SafetyLimits")
        if not isinstance(self.report_format, ReportFormat):
            raise ConfigurationError("report_format must be a ReportFormat")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ConfigurationError("random_seed must be an integer")
        if not isinstance(self.metrics, MetricsConfig):
            raise ConfigurationError("metrics must be a MetricsConfig")
        object.__setattr__(self, "warmup_seconds", warmup)
        object.__setattr__(self, "workers", workers)
        object.__setattr__(self, "max_connections", max_connections)
        if target_rate is not None:
            object.__setattr__(self, "target_rate", target_rate)
        if self.fault_policy is not None:
            if not isinstance(self.fault_policy, FaultPolicy):
                raise ConfigurationError("fault_policy must be a FaultPolicy")


@dataclass(frozen=True, slots=True)
class ResultSample:
    request_id: str
    scheduled_ns: int
    worker_start_ns: int
    pool_acquire_start_ns: int
    pool_acquire_end_ns: int | None
    write_start_ns: int | None
    write_end_ns: int | None
    first_byte_ns: int | None
    completion_ns: int
    status_code: int | None
    outcome: Outcome
    error_category: ErrorCategory | None
    bytes_sent: int = 0
    bytes_received: int = 0
    connection_reused: bool = False
    fault_applied: bool = False
    fault_mode: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        times = (
            ("scheduled_ns", self.scheduled_ns),
            ("worker_start_ns", self.worker_start_ns),
            ("pool_acquire_start_ns", self.pool_acquire_start_ns),
            ("completion_ns", self.completion_ns),
        )
        for field_name, value in times:
            _require_non_negative_int(value, field_name)
        for field_name in (
            "pool_acquire_end_ns",
            "write_start_ns",
            "write_end_ns",
            "first_byte_ns",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_negative_int(value, field_name)
        _check_order(self.scheduled_ns, self.worker_start_ns, "worker start")
        _check_order(self.worker_start_ns, self.pool_acquire_start_ns, "pool acquire")
        if self.pool_acquire_end_ns is not None:
            _check_order(self.pool_acquire_start_ns, self.pool_acquire_end_ns, "pool acquire end")
        if self.write_start_ns is not None and self.pool_acquire_end_ns is not None:
            _check_order(self.pool_acquire_end_ns, self.write_start_ns, "write start")
        if self.write_end_ns is not None and self.write_start_ns is not None:
            _check_order(self.write_start_ns, self.write_end_ns, "write end")
        if self.first_byte_ns is not None and self.write_end_ns is not None:
            _check_order(self.write_end_ns, self.first_byte_ns, "first byte")
        for timestamp in (
            self.pool_acquire_end_ns,
            self.write_start_ns,
            self.write_end_ns,
            self.first_byte_ns,
        ):
            if timestamp is not None:
                _check_order(timestamp, self.completion_ns, "completion")
        if not isinstance(self.outcome, Outcome):
            raise ConfigurationError("outcome must be an Outcome")
        if self.error_category is not None and not isinstance(self.error_category, ErrorCategory):
            raise ConfigurationError("error_category must be an ErrorCategory")
        if self.outcome in {Outcome.SUCCESS, Outcome.HTTP_ERROR}:
            if self.status_code is None:
                raise ConfigurationError("HTTP outcomes require status_code")
        elif self.status_code is not None:
            raise ConfigurationError("non-HTTP outcomes cannot have status_code")
        if self.outcome in {Outcome.SUCCESS, Outcome.HTTP_ERROR} and self.error_category is not None:
            raise ConfigurationError("HTTP outcomes cannot have error_category")
        if self.outcome not in {Outcome.SUCCESS, Outcome.HTTP_ERROR} and self.error_category is None:
            raise ConfigurationError("failure outcomes require error_category")
        if self.outcome is Outcome.CANCELLED and self.error_category is not ErrorCategory.CANCELLED:
            raise ConfigurationError("cancelled outcomes require cancelled error category")
        if self.status_code is not None:
            status_code = _require_positive_int(self.status_code, "status_code")
            if not 100 <= status_code <= 599:
                raise ConfigurationError("status_code must be between 100 and 599")
            object.__setattr__(self, "status_code", status_code)
        _require_non_negative_int(self.bytes_sent, "bytes_sent")
        _require_non_negative_int(self.bytes_received, "bytes_received")
        if not isinstance(self.connection_reused, bool):
            raise ConfigurationError("connection_reused must be a boolean")
        if self.pool_acquire_end_ns is None and self.connection_reused:
            raise ConfigurationError("an attempt without pool acquisition cannot reuse a connection")
        if not isinstance(self.fault_applied, bool):
            raise ConfigurationError("fault_applied must be a boolean")
        if self.fault_mode is not None and not isinstance(self.fault_mode, str):
            raise ConfigurationError("fault_mode must be a string or None")

    @property
    def request_latency_ns(self) -> int | None:
        if self.write_start_ns is None:
            return None
        return self.completion_ns - self.write_start_ns

    @property
    def end_to_end_latency_ns(self) -> int:
        return self.completion_ns - self.scheduled_ns

    @property
    def pool_wait_ns(self) -> int | None:
        if self.pool_acquire_end_ns is None:
            return None
        return self.pool_acquire_end_ns - self.pool_acquire_start_ns

    @property
    def time_to_first_byte_ns(self) -> int | None:
        if self.write_end_ns is None or self.first_byte_ns is None:
            return None
        return self.first_byte_ns - self.write_end_ns


def _check_order(start: int, end: int, label: str) -> None:
    if end < start:
        raise ConfigurationError(f"{label} timestamp precedes its start")
