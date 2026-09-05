"""Bounded, thread-safe connection pool for one HTTP origin."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

from ..domain.errors import ConnectionClosedEarly, ConfigurationError, PoolAcquireTimeout
from ..domain.models import Origin, SafetyLimits, TimeoutConfig
from ..http.session import Http1Session
from .lease import ConnectionLease


SessionFactory = Callable[[], Http1Session]


class ConnectionPool:
    """Lazily create and exclusively lease sessions for one origin."""

    def __init__(
        self,
        origin: Origin,
        max_connections: int,
        timeouts: TimeoutConfig,
        limits: SafetyLimits | None = None,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if not isinstance(origin, Origin):
            raise TypeError("origin must be an Origin")
        if isinstance(max_connections, bool) or not isinstance(max_connections, int):
            raise TypeError("max_connections must be an integer")
        selected_limits = limits or SafetyLimits()
        if not isinstance(selected_limits, SafetyLimits):
            raise TypeError("limits must be a SafetyLimits")
        if max_connections <= 0:
            raise ConfigurationError("max_connections must be positive")
        if max_connections > selected_limits.max_connections:
            raise ConfigurationError("max_connections exceeds configured safety limit")
        if not isinstance(timeouts, TimeoutConfig):
            raise TypeError("timeouts must be a TimeoutConfig")

        self._origin = origin
        self._max_connections = max_connections
        self._timeouts = timeouts
        self._limits = selected_limits
        self._factory = session_factory or self._default_factory
        self._condition = threading.Condition()
        self._idle: list[Http1Session] = []
        self._leased: set[Http1Session] = set()
        self._live = 0
        self._closed = False

    @property
    def origin(self) -> Origin:
        return self._origin

    @property
    def max_connections(self) -> int:
        return self._max_connections

    @property
    def live_connections(self) -> int:
        with self._condition:
            return self._live

    @property
    def idle_connections(self) -> int:
        with self._condition:
            return len(self._idle)

    @property
    def leased_connections(self) -> int:
        with self._condition:
            return len(self._leased)

    def acquire(self, deadline_ns: int | None = None) -> ConnectionLease:
        _validate_deadline(deadline_ns)

        while True:
            with self._condition:
                if self._closed:
                    raise ConnectionClosedEarly("connection pool is closed")
                if deadline_ns is not None:
                    _remaining_seconds(deadline_ns)

                while self._idle:
                    session = self._idle.pop()
                    if session.reusable:
                        self._leased.add(session)
                        return ConnectionLease(self, session)
                    session.close()
                    self._live -= 1

                if self._live < self._max_connections:
                    self._live += 1
                    break

                timeout = _remaining_seconds(deadline_ns)
                self._condition.wait(timeout)

        try:
            session = self._factory()
            if not _is_session(session):
                raise TypeError("session factory must return an HTTP session")
        except Exception:
            with self._condition:
                self._live -= 1
                self._condition.notify()
            raise

        with self._condition:
            if self._closed:
                self._live -= 1
                self._condition.notify_all()
                session.close()
                raise ConnectionClosedEarly("connection pool closed during acquisition")
            if deadline_ns is not None and deadline_ns <= time.monotonic_ns():
                self._live -= 1
                self._condition.notify()
                session.close()
                raise PoolAcquireTimeout("pool acquire deadline exceeded")
            if not session.reusable:
                self._live -= 1
                self._condition.notify()
                session.close()
                raise ConnectionClosedEarly("session factory returned a non-reusable session")
            self._leased.add(session)
            return ConnectionLease(self, session)

    def close_all(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            sessions = [*self._idle, *self._leased]
            self._idle.clear()
            self._leased.clear()
            self._live = 0
            self._condition.notify_all()
        for session in sessions:
            session.close()

    def _release(self, session: Http1Session, reusable: bool) -> None:
        with self._condition:
            if session not in self._leased:
                return
            self._leased.remove(session)
            self._live -= 1
            if not self._closed and reusable and session.reusable:
                self._idle.append(session)
                self._live += 1
            else:
                session.close()
            self._condition.notify()

    def _default_factory(self) -> Http1Session:
        return Http1Session.connect(
            self._origin,
            self._timeouts,
            self._limits,
        )


def _is_session(value: object) -> bool:
    return (
        callable(getattr(value, "close", None))
        and isinstance(getattr(value, "reusable", None), bool)
    )


def _validate_deadline(deadline_ns: int | None) -> None:
    if deadline_ns is not None and (
        isinstance(deadline_ns, bool) or not isinstance(deadline_ns, int)
    ):
        raise TypeError("deadline_ns must be an integer or None")


def _remaining_seconds(deadline_ns: int | None) -> float | None:
    if deadline_ns is None:
        return None
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise PoolAcquireTimeout("pool acquire deadline exceeded")
    return remaining_ns / 1_000_000_000
