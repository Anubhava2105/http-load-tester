"""TLS transport layered on the TCP socket adapter."""

from __future__ import annotations

import ssl
import time

from ..domain.errors import TlsFailure
from ..domain.models import Origin, TimeoutConfig
from .tcp import TcpTransport


def create_ssl_context(origin: Origin) -> ssl.SSLContext:
    """Create a verifying context unless the origin explicitly disables it."""
    if not isinstance(origin, Origin):
        raise TypeError("origin must be an Origin")
    if origin.scheme != "https":
        raise ValueError("TLS requires an https origin")

    context = ssl.create_default_context()
    if not origin.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class TlsTransport(TcpTransport):
    """A TCP transport whose socket completes a verified TLS handshake."""

    @classmethod
    def connect(cls, origin: Origin, timeouts: TimeoutConfig) -> TlsTransport:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be an Origin")
        if not isinstance(timeouts, TimeoutConfig):
            raise TypeError("timeouts must be a TimeoutConfig")

        raw_socket = cls._connect_socket(origin, timeouts)
        wrapped_socket: ssl.SSLSocket | None = None
        try:
            context = create_ssl_context(origin)
            wrapped_socket = context.wrap_socket(
                raw_socket,
                server_hostname=origin.server_name or origin.hostname,
                do_handshake_on_connect=False,
            )
            deadline_ns = time.monotonic_ns() + int(
                timeouts.tls_handshake_seconds * 1_000_000_000
            )
            cls._set_timeout(wrapped_socket, deadline_ns, TlsFailure)
            wrapped_socket.do_handshake()
            wrapped_socket.settimeout(None)
            return cls(wrapped_socket)
        except (TlsFailure, OSError, ssl.SSLError, TimeoutError, ValueError) as exc:
            if wrapped_socket is not None:
                try:
                    wrapped_socket.close()
                except OSError:
                    pass
            else:
                try:
                    raw_socket.close()
                except OSError:
                    pass
            raise TlsFailure(f"TLS handshake failed for {origin.hostname}") from exc
