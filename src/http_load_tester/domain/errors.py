"""Stable error categories and typed internal exceptions."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    CONFIGURATION_ERROR = "configuration_error"
    INVALID_TARGET = "invalid_target"
    DNS_FAILURE = "dns_failure"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECTION_REFUSED = "connection_refused"
    TRANSPORT_FAILURE = "transport_failure"
    TLS_FAILURE = "tls_failure"
    WRITE_TIMEOUT = "write_timeout"
    READ_TIMEOUT = "read_timeout"
    CONNECTION_RESET = "connection_reset"
    CONNECTION_CLOSED_EARLY = "connection_closed_early"
    PROTOCOL_ERROR = "protocol_error"
    INVALID_HEADERS = "invalid_headers"
    INVALID_BODY_FRAMING = "invalid_body_framing"
    RESPONSE_TOO_LARGE = "response_too_large"
    POOL_ACQUIRE_TIMEOUT = "pool_acquire_timeout"
    CANCELLED = "cancelled"


class RawLoadError(Exception):
    """Base class for errors that receive a stable report category."""

    category = ErrorCategory.PROTOCOL_ERROR

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class ConfigurationError(RawLoadError):
    category = ErrorCategory.CONFIGURATION_ERROR


class InvalidTarget(RawLoadError):
    category = ErrorCategory.INVALID_TARGET


class DnsFailure(RawLoadError):
    category = ErrorCategory.DNS_FAILURE


class ConnectTimeout(RawLoadError):
    category = ErrorCategory.CONNECT_TIMEOUT


class ConnectionRefused(RawLoadError):
    category = ErrorCategory.CONNECTION_REFUSED


class TransportFailure(RawLoadError):
    category = ErrorCategory.TRANSPORT_FAILURE


class TlsFailure(RawLoadError):
    category = ErrorCategory.TLS_FAILURE


class WriteTimeout(RawLoadError):
    category = ErrorCategory.WRITE_TIMEOUT


class ReadTimeout(RawLoadError):
    category = ErrorCategory.READ_TIMEOUT


class ConnectionReset(RawLoadError):
    category = ErrorCategory.CONNECTION_RESET


class ConnectionClosedEarly(RawLoadError):
    category = ErrorCategory.CONNECTION_CLOSED_EARLY


class ProtocolError(RawLoadError):
    category = ErrorCategory.PROTOCOL_ERROR


class InvalidHeaders(ProtocolError):
    category = ErrorCategory.INVALID_HEADERS


class InvalidBodyFraming(ProtocolError):
    category = ErrorCategory.INVALID_BODY_FRAMING


class ResponseTooLarge(ProtocolError):
    category = ErrorCategory.RESPONSE_TOO_LARGE


class PoolAcquireTimeout(RawLoadError):
    category = ErrorCategory.POOL_ACQUIRE_TIMEOUT


class Cancelled(RawLoadError):
    category = ErrorCategory.CANCELLED
