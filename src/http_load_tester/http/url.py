"""Parse HTTP targets into connection origins and request targets."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ..domain.errors import InvalidTarget
from ..domain.models import Origin


@dataclass(frozen=True, slots=True)
class ParsedTarget:
    """The connection origin and origin-form target derived from a URL."""

    origin: Origin
    request_target: str


def parse_target(
    raw_url: str,
    *,
    tls_verify: bool = True,
    server_name: str | None = None,
) -> ParsedTarget:
    """Parse one absolute HTTP or HTTPS URL without opening a connection."""

    if not isinstance(raw_url, str) or not raw_url:
        raise InvalidTarget("target URL must be a non-empty string")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw_url):
        raise InvalidTarget("target URL must not contain whitespace or control characters")
    try:
        parts = urlsplit(raw_url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidTarget(f"invalid target URL: {exc}", cause=exc) from exc
    if scheme not in {"http", "https"}:
        raise InvalidTarget("target URL scheme must be http or https")
    if not parts.netloc:
        raise InvalidTarget("target URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise InvalidTarget("target URL user information is unsupported")
    if hostname is None or not hostname:
        raise InvalidTarget("target URL must include a hostname")
    if port is None:
        port = 443 if scheme == "https" else 80
    try:
        origin = Origin(
            scheme=scheme,
            hostname=hostname,
            port=port,
            tls_verify=tls_verify,
            server_name=server_name,
        )
    except Exception as exc:
        if isinstance(exc, InvalidTarget):
            raise
        raise InvalidTarget(f"invalid target origin: {exc}", cause=exc) from exc
    request_target = parts.path or "/"
    if parts.query:
        request_target += "?" + parts.query
    return ParsedTarget(origin=origin, request_target=request_target)
