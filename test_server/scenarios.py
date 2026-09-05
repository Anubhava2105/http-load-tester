"""Deterministic HTTP response scenarios for local tests and demonstrations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scenario(StrEnum):
    FIXED = "fixed"
    CHUNKED = "chunked"
    DELAYED_HEADERS = "delayed_headers"
    DELAYED_BODY = "delayed_body"
    FORCED_CLOSE = "forced_close"
    TRUNCATED = "truncated"
    LARGE_RESPONSE = "large_response"
    INTERMITTENT_500 = "intermittent_500"


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    scenario: Scenario = Scenario.FIXED
    body: bytes = b"ok"
    delay_seconds: float = 0.05
    chunk_size: int = 2
    large_body_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, Scenario):
            raise ValueError("scenario must be a Scenario")
        if not isinstance(self.body, bytes):
            raise ValueError("body must be bytes")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.large_body_bytes < 0:
            raise ValueError("large_body_bytes must be non-negative")


def response_headers(status: int, length: int, *, close: bool = False) -> bytes:
    reason = "OK" if status == 200 else "Internal Server Error"
    connection = b"close" if close else b"keep-alive"
    return (
        f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
        + f"Content-Length: {length}\r\n".encode("ascii")
        + b"Connection: "
        + connection
        + b"\r\n\r\n"
    )


def chunked_response(body: bytes, *, close: bool = False, chunk_size: int = 2) -> bytes:
    chunks = []
    for offset in range(0, len(body), chunk_size):
        chunk = body[offset : offset + chunk_size]
        chunks.append(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
    chunks.append(b"0\r\n\r\n")
    connection = b"close" if close else b"keep-alive"
    return (
        b"HTTP/1.1 200 OK\r\n"
        + b"Transfer-Encoding: chunked\r\nConnection: "
        + connection
        + b"\r\n\r\n"
        + b"".join(chunks)
    )
