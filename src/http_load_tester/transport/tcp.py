"""Deadline-aware blocking TCP transport."""

from __future__ import annotations

import errno
import socket
import time

from ..domain.errors import (
    ConnectionClosedEarly,
    ConnectionRefused,
    ConnectionReset,
    ConnectTimeout,
    DnsFailure,
    ReadTimeout,
    TransportFailure,
    WriteTimeout,
)
from ..domain.models import Origin, TimeoutConfig
from .interface import Deadline


class TcpTransport:
    """A small socket adapter with absolute deadlines at its public boundary."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._closed = False

    @classmethod
    def connect(cls, origin: Origin, timeouts: TimeoutConfig) -> TcpTransport:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be an Origin")
        if not isinstance(timeouts, TimeoutConfig):
            raise TypeError("timeouts must be a TimeoutConfig")
        return cls(cls._connect_socket(origin, timeouts))

    @staticmethod
    def _connect_socket(origin: Origin, timeouts: TimeoutConfig) -> socket.socket:
        dns_started_ns = time.monotonic_ns()
        try:
            addresses = socket.getaddrinfo(
                origin.hostname,
                origin.port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise DnsFailure(f"DNS lookup failed for {origin.hostname}") from exc

        dns_elapsed_ns = time.monotonic_ns() - dns_started_ns
        dns_deadline_ns = int(timeouts.dns_seconds * 1_000_000_000)
        if dns_elapsed_ns > dns_deadline_ns:
            raise DnsFailure(f"DNS lookup timed out for {origin.hostname}")
        if not addresses:
            raise DnsFailure(f"DNS lookup returned no addresses for {origin.hostname}")

        deadline_ns = time.monotonic_ns() + int(
            timeouts.connect_seconds * 1_000_000_000
        )
        last_error: Exception | None = None

        for family, socktype, proto, _, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            connected = False
            try:
                TcpTransport._set_timeout(sock, deadline_ns, ConnectTimeout)
                sock.connect(sockaddr)
                sock.settimeout(None)
                connected = True
                return sock
            except socket.timeout as exc:
                last_error = ConnectTimeout(f"connect timed out to {origin.hostname}")
            except ConnectionRefusedError as exc:
                last_error = ConnectionRefused(
                    f"connection refused by {origin.hostname}:{origin.port}"
                )
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    last_error = ConnectionRefused(
                        f"connection refused by {origin.hostname}:{origin.port}"
                    )
                elif exc.errno in {
                    errno.ECONNABORTED,
                    errno.ECONNRESET,
                }:
                    last_error = ConnectionReset(
                        f"connection reset while connecting to {origin.hostname}"
                    )
                else:
                    last_error = TransportFailure(
                        f"connection failed to {origin.hostname}:{origin.port}: {exc}"
                    )
            finally:
                if not connected:
                    sock.close()

        if last_error is not None:
            raise last_error
        raise TransportFailure(f"unable to connect to {origin.hostname}:{origin.port}")

    def send_all(self, data: bytes, deadline_ns: Deadline = None) -> None:
        if self._closed:
            raise ConnectionClosedEarly("cannot write to a closed transport")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")

        offset = 0
        while offset < len(data):
            self._set_timeout(self._socket, deadline_ns, WriteTimeout)
            try:
                sent = self._socket.send(data[offset:])
            except socket.timeout as exc:
                raise WriteTimeout("socket write timed out") from exc
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as exc:
                raise ConnectionReset("connection reset during write") from exc
            except OSError as exc:
                raise ConnectionReset(f"socket write failed: {exc}") from exc
            if sent <= 0:
                raise ConnectionClosedEarly("socket closed during write")
            offset += sent

    def recv_some(self, max_bytes: int, deadline_ns: Deadline = None) -> bytes:
        if self._closed:
            raise ConnectionClosedEarly("cannot read from a closed transport")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")

        self._set_timeout(self._socket, deadline_ns, ReadTimeout)
        try:
            return self._socket.recv(max_bytes)
        except socket.timeout as exc:
            raise ReadTimeout("socket read timed out") from exc
        except (ConnectionAbortedError, ConnectionResetError) as exc:
            raise ConnectionReset("connection reset during read") from exc
        except OSError as exc:
            raise ConnectionReset(f"socket read failed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass

    @staticmethod
    def _set_timeout(
        sock: socket.socket,
        deadline_ns: Deadline,
        error_type: type[Exception],
    ) -> None:
        if deadline_ns is None:
            sock.settimeout(None)
            return

        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            raise error_type("transport deadline exceeded")
        sock.settimeout(remaining_ns / 1_000_000_000)
