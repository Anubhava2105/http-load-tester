"""HTTP/1.1 session coordinating request and response framing."""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading

from ..domain.clock import Clock, MonotonicClock
from ..domain.errors import ConnectionClosedEarly
from ..domain.models import HttpRequest, HttpResponseSummary, Origin, SafetyLimits, TimeoutConfig
from ..transport.interface import Deadline, Transport
from ..transport.tcp import TcpTransport
from ..transport.tls import TlsTransport
from .request_encoder import encode_request
from .response_parser import ResponseParser, ResponseReader


@dataclass(frozen=True, slots=True)
class SessionTiming:
    """Monotonic timestamps collected for one request exchange."""

    write_start_ns: int
    write_end_ns: int | None
    first_byte_ns: int | None
    completion_ns: int


class Http1Session:
    """Execute one non-pipelined HTTP/1.1 exchange at a time."""

    def __init__(
        self,
        origin: Origin,
        transport: Transport,
        limits: SafetyLimits | None = None,
        *,
        parser: ResponseParser | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be an Origin")
        if not all(
            callable(getattr(transport, method, None))
            for method in ("send_all", "recv_some", "close")
        ):
            raise TypeError("transport must implement Transport")
        if limits is not None and not isinstance(limits, SafetyLimits):
            raise TypeError("limits must be a SafetyLimits")
        self._origin = origin
        self._transport = transport
        self._limits = limits or SafetyLimits()
        self._parser = parser or ResponseParser()
        self._clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._reusable = True
        self._closed = False
        self._last_timing: SessionTiming | None = None

    @classmethod
    def connect(
        cls,
        origin: Origin,
        timeouts: TimeoutConfig,
        limits: SafetyLimits | None = None,
        *,
        clock: Clock | None = None,
    ) -> Http1Session:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be an Origin")
        if not isinstance(timeouts, TimeoutConfig):
            raise TypeError("timeouts must be a TimeoutConfig")
        transport: Transport
        if origin.scheme == "https":
            transport = TlsTransport.connect(origin, timeouts)
        else:
            transport = TcpTransport.connect(origin, timeouts)
        return cls(origin, transport, limits, clock=clock)

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def reusable(self) -> bool:
        return self._reusable and not self._closed

    @property
    def last_timing(self) -> SessionTiming | None:
        return self._last_timing

    def execute(
        self,
        request: HttpRequest,
        deadline_ns: Deadline = None,
    ) -> HttpResponseSummary:
        if not isinstance(request, HttpRequest):
            raise TypeError("request must be an HttpRequest")

        encoded = encode_request(request, self._origin)
        request_wants_close = _has_connection_token(request.headers, "close")
        request_is_upgrade = _has_header(request.headers, "upgrade")
        with self._lock:
            if self._closed or not self._reusable:
                raise ConnectionClosedEarly("session is no longer reusable")

            write_start_ns = self._clock.now_ns()
            write_end_ns: int | None = None
            first_byte_ns: int | None = None
            timed_reader: _TimedReader | None = None
            try:
                self._transport.send_all(encoded, deadline_ns)
                write_end_ns = self._clock.now_ns()
                timed_reader = _TimedReader(self._transport, self._clock)
                response = self._parser.parse_response(
                    timed_reader,
                    self._limits,
                    deadline_ns,
                )
                first_byte_ns = timed_reader.first_byte_ns
                unsupported_upgrade = (
                    response.status_code == 101
                    or _has_header(response.headers, "upgrade")
                    or _has_connection_token(response.headers, "upgrade")
                )
                if (
                    request_wants_close
                    or request_is_upgrade
                    or unsupported_upgrade
                    or not response.connection_reusable
                ):
                    self._reusable = False
                    response = replace(response, connection_reusable=False)
                return response
            except Exception:
                self._reusable = False
                raise
            finally:
                if timed_reader is not None:
                    first_byte_ns = timed_reader.first_byte_ns
                self._last_timing = SessionTiming(
                    write_start_ns=write_start_ns,
                    write_end_ns=write_end_ns,
                    first_byte_ns=first_byte_ns,
                    completion_ns=self._clock.now_ns(),
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reusable = False
            self._transport.close()


class _TimedReader(ResponseReader):
    def __init__(self, transport: Transport, clock: Clock) -> None:
        self._transport = transport
        self._clock = clock
        self.first_byte_ns: int | None = None

    def recv_some(self, max_bytes: int, deadline_ns: Deadline = None) -> bytes:
        chunk = self._transport.recv_some(max_bytes, deadline_ns)
        if chunk and self.first_byte_ns is None:
            self.first_byte_ns = self._clock.now_ns()
        return chunk


def _has_header(headers: tuple[tuple[str, str], ...], name: str) -> bool:
    lowered = name.lower()
    return any(header_name.lower() == lowered for header_name, _ in headers)


def _has_connection_token(headers: tuple[tuple[str, str], ...], token: str) -> bool:
    lowered = token.lower()
    return any(
        part.strip().lower() == lowered
        for header_name, value in headers
        if header_name.lower() == "connection"
        for part in value.split(",")
    )
