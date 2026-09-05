"""Buffered HTTP/1.1 response parser independent of socket details."""

from __future__ import annotations

import re
from typing import Protocol

from ..domain.errors import (
    ConnectionClosedEarly,
    InvalidBodyFraming,
    InvalidHeaders,
    ProtocolError,
    ResponseTooLarge,
)
from ..domain.models import Headers, HttpResponseSummary, SafetyLimits

Deadline = int | None



class ResponseReader(Protocol):
    def recv_some(self, max_bytes: int, deadline_ns: Deadline = None) -> bytes:
        """Return up to max_bytes or b"" at orderly EOF."""


_HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+$")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_NO_BODY_STATUSES = frozenset({204, 304})


class ResponseParser:
    """Parse successive responses while retaining bytes read past each boundary."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def parse_response(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline = None,
    ) -> HttpResponseSummary:
        if not isinstance(limits, SafetyLimits):
            raise TypeError("limits must be a SafetyLimits")

        status_line, headers = self._read_headers(reader, limits, deadline_ns)
        version, status_code, reason = self._parse_status_line(status_line)
        self._validate_framing_headers(headers)
        transfer_encoding = _header_values(headers, "transfer-encoding")
        content_length = _header_values(headers, "content-length")

        if 100 <= status_code < 200 or status_code in _NO_BODY_STATUSES:
            body_bytes = 0
            body_complete = True
        else:
            if transfer_encoding:
                body_bytes = self._read_chunked_body(reader, limits, deadline_ns)
                body_complete = True
            elif content_length:
                declared_length = _parse_content_length(content_length[0])
                if declared_length > limits.max_response_body_bytes:
                    raise ResponseTooLarge(
                        "declared response body exceeds max_response_body_bytes"
                    )
                self._discard_exact(
                    reader,
                    declared_length,
                    deadline_ns,
                    "response body ended before Content-Length was satisfied",
                )
                body_bytes = declared_length
                body_complete = True
            else:
                body_bytes = self._read_close_delimited_body(
                    reader,
                    limits,
                    deadline_ns,
                )
                body_complete = True

        body_is_self_delimiting = (
            bool(content_length)
            or bool(transfer_encoding)
            or 100 <= status_code < 200
            or status_code in _NO_BODY_STATUSES
        )
        return HttpResponseSummary(
            http_version=version,
            status_code=status_code,
            reason=reason,
            headers=headers,
            body_bytes=body_bytes,
            body_complete=body_complete,
            connection_reusable=(
                self._can_reuse(version, status_code, headers)
                and body_is_self_delimiting
            ),

        )
    def _read_headers(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline,
    ) -> tuple[bytes, Headers]:
        marker = b"\r\n\r\n"
        while True:
            header_end = self._buffer.find(marker)
            if header_end >= 0:
                header_size = header_end + len(marker)
                if header_size > limits.max_header_bytes:
                    raise ResponseTooLarge("response headers exceed max_header_bytes")
                block = bytes(self._buffer[:header_size])
                del self._buffer[:header_size]
                return self._parse_header_block(block, limits)

            if len(self._buffer) >= limits.max_header_bytes:
                raise ResponseTooLarge("response headers exceed max_header_bytes")
            if not self._read_more(reader, deadline_ns):
                raise ConnectionClosedEarly("response ended before headers were complete")

    def _parse_header_block(
        self,
        block: bytes,
        limits: SafetyLimits,
    ) -> tuple[bytes, Headers]:
        lines = block[:-4].split(b"\r\n")
        if not lines or not lines[0]:
            raise ProtocolError("response is missing a status line")

        status_line = lines[0]
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                raise InvalidHeaders("response contains an empty header line")
            if len(headers) >= limits.max_header_count:
                raise ResponseTooLarge("response exceeds max_header_count")
            headers.append(_parse_header_line(line))
        return status_line, tuple(headers)

    def _parse_status_line(self, status_line: bytes) -> tuple[str, int, str]:
        try:
            text = status_line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("status line must be ASCII") from exc

        parts = text.split(" ", 2)
        if len(parts) < 2 or parts[0] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProtocolError("invalid HTTP status line")
        status_text = parts[1]
        if len(status_text) != 3 or not status_text.isdecimal():
            raise ProtocolError("status code must be three decimal digits")
        status_code = int(status_text)
        if not 100 <= status_code <= 599:
            raise ProtocolError("status code is outside the supported range")
        reason = parts[2] if len(parts) == 3 else ""
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in reason):
            raise ProtocolError("reason phrase contains a control character")
        return parts[0], status_code, reason

    def _validate_framing_headers(self, headers: Headers) -> None:
        content_lengths = _header_values(headers, "content-length")
        if len(content_lengths) > 1:
            raise InvalidBodyFraming("duplicate Content-Length headers are unsupported")

        transfer_encoding = _header_values(headers, "transfer-encoding")
        if content_lengths and transfer_encoding:
            raise InvalidBodyFraming(
                "Content-Length and Transfer-Encoding cannot appear together"
            )
        if transfer_encoding:
            codings = [
                coding.strip().lower()
                for value in transfer_encoding
                for coding in value.split(",")
            ]
            if not codings or any(not coding for coding in codings):
                raise InvalidBodyFraming("Transfer-Encoding contains an empty coding")
            if codings[-1] != "chunked":
                raise InvalidBodyFraming(
                    "the final Transfer-Encoding coding must be chunked"
                )

    def _read_chunked_body(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline,
    ) -> int:
        total = 0
        while True:
            line = self._read_line(reader, limits, deadline_ns)
            size_text = line.split(b";", 1)[0].strip()
            if not size_text or any(byte not in _HEX_DIGITS for byte in size_text):
                raise InvalidBodyFraming("invalid chunk-size line")
            chunk_size = int(size_text, 16)
            if chunk_size > limits.max_response_body_bytes - total:
                raise ResponseTooLarge("chunked response exceeds max_response_body_bytes")

            if chunk_size == 0:
                self._read_trailers(reader, limits, deadline_ns)
                return total

            self._discard_exact(
                reader,
                chunk_size,
                deadline_ns,
                "chunked response ended before chunk data was complete",
            )
            total += chunk_size
            delimiter = self._read_exact(reader, 2, deadline_ns)
            if delimiter != b"\r\n":
                raise InvalidBodyFraming("chunk data is not followed by CRLF")

    def _read_trailers(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline,
    ) -> None:
        trailer_count = 0
        while True:
            line = self._read_line(reader, limits, deadline_ns)
            if not line:
                return
            trailer_count += 1
            if trailer_count > limits.max_header_count:
                raise ResponseTooLarge("response trailers exceed max_header_count")
            _parse_header_line(line)

    def _read_close_delimited_body(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline,
    ) -> int:
        total = len(self._buffer)
        self._buffer.clear()
        if total > limits.max_response_body_bytes:
            raise ResponseTooLarge("response body exceeds max_response_body_bytes")

        while True:
            chunk = reader.recv_some(4096, deadline_ns)
            if not isinstance(chunk, bytes):
                raise ProtocolError("response reader must return bytes")
            if not chunk:
                return total
            total += len(chunk)
            if total > limits.max_response_body_bytes:
                raise ResponseTooLarge("response body exceeds max_response_body_bytes")

    def _discard_exact(
        self,
        reader: ResponseReader,
        amount: int,
        deadline_ns: Deadline,
        eof_message: str,
    ) -> None:
        remaining = amount
        while remaining:
            available = min(remaining, len(self._buffer))
            if available:
                del self._buffer[:available]
                remaining -= available
                continue
            chunk = self._read_more(reader, deadline_ns)
            if not chunk:
                raise ConnectionClosedEarly(eof_message)

    def _read_exact(
        self,
        reader: ResponseReader,
        amount: int,
        deadline_ns: Deadline,
    ) -> bytes:
        result = bytearray()
        while len(result) < amount:
            available = min(amount - len(result), len(self._buffer))
            if available:
                result.extend(self._buffer[:available])
                del self._buffer[:available]
                continue
            self._read_more(reader, deadline_ns)
            if not self._buffer:
                raise ConnectionClosedEarly("response ended before expected bytes arrived")
        return bytes(result)

    def _read_line(
        self,
        reader: ResponseReader,
        limits: SafetyLimits,
        deadline_ns: Deadline,
    ) -> bytes:
        while True:
            line_end = self._buffer.find(b"\r\n")
            if line_end >= 0:
                line = bytes(self._buffer[:line_end])
                del self._buffer[: line_end + 2]
                return line
            if len(self._buffer) >= limits.max_header_bytes:
                raise ResponseTooLarge("response control line exceeds max_header_bytes")
            if not self._read_more(reader, deadline_ns):
                raise ConnectionClosedEarly("response ended before control line was complete")

    def _read_more(self, reader: ResponseReader, deadline_ns: Deadline) -> bytes:
        chunk = reader.recv_some(4096, deadline_ns)
        if not isinstance(chunk, bytes):
            raise ProtocolError("response reader must return bytes")
        if chunk:
            self._buffer.extend(chunk)
        return chunk

    @staticmethod
    def _can_reuse(version: str, status_code: int, headers: Headers) -> bool:
        if status_code == 101:
            return False
        connection_tokens = _header_tokens(headers, "connection")
        if "close" in connection_tokens or "upgrade" in connection_tokens:
            return False
        if version == "HTTP/1.1":
            return True
        return "keep-alive" in connection_tokens


def parse_response(
    reader: ResponseReader,
    limits: SafetyLimits,
    deadline_ns: Deadline = None,
    parser: ResponseParser | None = None,
) -> HttpResponseSummary:
    """Parse one response, optionally using a parser retained across calls."""
    return (parser or ResponseParser()).parse_response(reader, limits, deadline_ns)


def _parse_header_line(line: bytes) -> tuple[str, str]:
    if line[:1] in {b" ", b"\t"} or b":" not in line:
        raise InvalidHeaders("malformed response header line")
    name_bytes, value_bytes = line.split(b":", 1)
    if not _HEADER_NAME_RE.fullmatch(name_bytes):
        raise InvalidHeaders("invalid response header name")
    try:
        name = name_bytes.decode("ascii")
        value = value_bytes.decode("latin-1").strip(" \t")
    except UnicodeDecodeError as exc:
        raise InvalidHeaders("response headers use invalid encoding") from exc
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise InvalidHeaders("response header contains a control character")
    return name, value


def _header_values(headers: Headers, name: str) -> list[str]:
    lowered = name.lower()
    return [value for header_name, value in headers if header_name.lower() == lowered]


def _header_tokens(headers: Headers, name: str) -> set[str]:
    return {
        token.strip().lower()
        for value in _header_values(headers, name)
        for token in value.split(",")
        if token.strip()
    }


def _parse_content_length(value: str) -> int:
    stripped = value.strip()
    if not stripped or any(character not in "0123456789" for character in stripped):
        raise InvalidBodyFraming("Content-Length must be a non-negative decimal integer")
    return int(stripped)
