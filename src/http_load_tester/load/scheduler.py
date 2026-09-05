"""Closed-loop request scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import threading
import time

from ..domain.clock import Clock, MonotonicClock
from ..domain.models import TestPlan


@dataclass(frozen=True, slots=True)
class ScheduledAttempt:
    request_id: str
    scheduled_ns: int


class ClosedLoopScheduler:
    """Issue the next attempt only after a worker asks for more work."""

    def __init__(self, plan: TestPlan, *, clock: Clock | None = None, sleeper: Callable[[float], None] | None = None) -> None:
        if not isinstance(plan, TestPlan):
            raise TypeError("plan must be a TestPlan")
        self._plan = plan
        self._sleeper = sleeper or time.sleep
        self._clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._next_sequence = 1
        self._started_ns: int | None = None
        self._end_ns: int | None = None

    @property
    def started_ns(self) -> int | None:
        return self._started_ns

    def start(self) -> None:
        with self._lock:
            if self._started_ns is not None:
                return
            started_ns = self._clock.now_ns() + int(
                self._plan.warmup_seconds * 1_000_000_000
            )
            self._started_ns = started_ns
            if self._plan.duration_seconds is not None:
                self._end_ns = started_ns + int(
                    self._plan.duration_seconds * 1_000_000_000
                )

    def wait_until_start(self) -> None:
        self.start()
        while True:
            started_ns = self._started_ns
            if started_ns is None:
                return
            remaining_ns = started_ns - self._clock.now_ns()
            if remaining_ns <= 0:
                return
            self._sleeper(remaining_ns / 1_000_000_000)

    def next_attempt(self) -> ScheduledAttempt | None:
        self.start()
        if self._stop_event.is_set():
            return None
        with self._lock:
            if self._stop_event.is_set():
                return None
            now_ns = self._clock.now_ns()
            if self._end_ns is not None and now_ns >= self._end_ns:
                return None
            if (
                self._plan.request_count is not None
                and self._next_sequence > self._plan.request_count
            ):
                return None
            sequence = self._next_sequence
            self._next_sequence += 1
            return ScheduledAttempt(f"request-{sequence}", now_ns)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()
class OpenLoopScheduler:
    """Issue attempts on a monotonic target-rate timeline."""

    def __init__(
        self,
        plan: TestPlan,
        *,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(plan, TestPlan):
            raise TypeError("plan must be a TestPlan")
        if plan.target_rate is None:
            raise ValueError("open-loop scheduling requires plan.target_rate")
        self._plan = plan
        self._clock = clock or MonotonicClock()
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._next_sequence = 1
        self._started_ns: int | None = None
        self._end_ns: int | None = None

    @property
    def started_ns(self) -> int | None:
        return self._started_ns

    @property
    def target_rate(self) -> float:
        return self._plan.target_rate or 0.0

    def start(self) -> None:
        with self._lock:
            if self._started_ns is not None:
                return
            started_ns = self._clock.now_ns() + int(
                self._plan.warmup_seconds * 1_000_000_000
            )
            self._started_ns = started_ns
            if self._plan.duration_seconds is not None:
                self._end_ns = started_ns + int(
                    self._plan.duration_seconds * 1_000_000_000
                )

    def wait_until_start(self) -> None:
        self.start()
        while True:
            started_ns = self._started_ns
            if started_ns is None:
                return
            remaining_ns = started_ns - self._clock.now_ns()
            if remaining_ns <= 0 or self._stop_event.is_set():
                return
            if self._sleeper is not None:
                self._sleeper(remaining_ns / 1_000_000_000)
            else:
                self._stop_event.wait(min(remaining_ns / 1_000_000_000, 0.1))

    def next_attempt(self) -> ScheduledAttempt | None:
        self.start()
        with self._lock:
            if self._stop_event.is_set():
                return None
            now_ns = self._clock.now_ns()
            if self._end_ns is not None and now_ns >= self._end_ns:
                return None
            if (
                self._plan.request_count is not None
                and self._next_sequence > self._plan.request_count
            ):
                return None
            sequence = self._next_sequence
            scheduled_ns = self._started_ns + int(
                (sequence - 1) * 1_000_000_000 / self.target_rate
            )
            if self._end_ns is not None and scheduled_ns >= self._end_ns:
                return None
            self._next_sequence += 1

        while True:
            remaining_ns = scheduled_ns - self._clock.now_ns()
            if remaining_ns <= 0:
                break
            if self._stop_event.is_set():
                return None
            if self._sleeper is not None:
                self._sleeper(remaining_ns / 1_000_000_000)
            elif self._stop_event.wait(min(remaining_ns / 1_000_000_000, 0.1)):
                return None

        if self._stop_event.is_set():
            return None
        return ScheduledAttempt(f"request-{sequence}", scheduled_ns)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()
