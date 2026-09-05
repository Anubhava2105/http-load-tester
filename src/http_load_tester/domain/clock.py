"""Clock seam used by scheduling and duration calculations."""

from __future__ import annotations

from typing import Protocol
import time


class Clock(Protocol):
    def now_ns(self) -> int:
        """Return a monotonic timestamp in nanoseconds."""


class MonotonicClock:
    """Production clock backed by perf_counter_ns."""

    def now_ns(self) -> int:
        return time.perf_counter_ns()
