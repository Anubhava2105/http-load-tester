"""Serialize validated requests using unambiguous HTTP/1.1 framing."""

from __future__ import annotations

from ..domain.errors import InvalidBodyFraming, InvalidHeaders, InvalidTarget
from ..domain.models import HttpRequest, Origin


def encode_request(request: HttpRequest, origin: Origin) -> bytes:
    """Return one complete HTTP/1.1 request as bytes."""

    if not isinstance(request, HttpRequest):
        raise TypeError("request must be an HttpRequest")
    if not isinstance(origin, Origin):
        raise TypeError("origin must be an Origin")
    try:
        request_line = f"{request.method} {request.target} HTTP/1.1\r\n".encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidTarget("method and target must be ASCII") from exc

    host_headers = _headers_named(request, "host")
    if len(host_headers) > 1:
        raise InvalidHeaders("request must contain at most one Host header")
    if host_headers and not host_headers[0][1].strip():
        raise InvalidHeaders("Host header must not be empty")

    transfer_encoding = _headers_named(request, "transfer-encoding")
    if transfer_encoding:
        raise InvalidBodyFraming("Transfer-Encoding is unsupported for materialized request bodies")

    content_lengths = _headers_named(request, "content-length")
    if len(content_lengths) > 1:
        raise InvalidBodyFraming("request must contain at most one Content-Length header")
    if content_lengths:
        declared = _parse_content_length(content_lengths[0][1])
        if declared != len(request.body):
            raise InvalidBodyFraming(
                f"Content-Length {declared} does not match body length {len(request.body)}"
            )

    headers: list[tuple[str, str]] = []
    if not host_headers:
        headers.append(("Host", _host_value(origin)))
    headers.extend(request.headers)
    if not _headers_named(request, "accept-encoding"):
        headers.append(("Accept-Encoding", "identity"))
    if not content_lengths:
        headers.append(("Content-Length", str(len(request.body))))

    try:
        encoded_headers = b"".join(
            name.encode("ascii") + b": " + value.encode("latin-1") + b"\r\n"
            for name, value in headers
        )
    except UnicodeEncodeError as exc:
        raise InvalidHeaders("HTTP header names must be ASCII and values Latin-1") from exc
    return request_line + encoded_headers + b"\r\n" + request.body


def _headers_named(request: HttpRequest, name: str) -> list[tuple[str, str]]:
    lowered = name.lower()
    return [(header_name, value) for header_name, value in request.headers if header_name.lower() == lowered]


def _parse_content_length(value: str) -> int:
    stripped = value.strip()
    if not stripped or any(character not in "0123456789" for character in stripped):
        raise InvalidBodyFraming("Content-Length must be a non-negative decimal integer")
    return int(stripped)


def _host_value(origin: Origin) -> str:
    hostname = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
    default_port = 443 if origin.scheme == "https" else 80
    return hostname if origin.port == default_port else f"{hostname}:{origin.port}"
