"""Small transport interface used by HTTP framing code."""

from __future__ import annotations

from typing import Protocol


Deadline = int | None


class Transport(Protocol):
    def send_all(self, data: bytes, deadline_ns: Deadline = None) -> None:
        """Send all bytes or raise a classified transport error."""

    def recv_some(self, max_bytes: int, deadline_ns: Deadline = None) -> bytes:
        """Receive up to max_bytes; b"" indicates an orderly EOF."""

    def close(self) -> None:
        """Close the transport; repeated calls are safe."""
