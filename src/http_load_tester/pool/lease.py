"""Lease lifecycle for pooled HTTP sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http.session import Http1Session
    from .connection_pool import ConnectionPool


class ConnectionLease:
    """Exclusive handle for one session borrowed from a connection pool."""

    def __init__(self, pool: ConnectionPool, connection: Http1Session) -> None:
        self._pool = pool
        self._connection = connection
        self._released = False

    @property
    def connection(self) -> Http1Session:
        return self._connection

    @property
    def released(self) -> bool:
        return self._released

    def release(self, reusable: bool) -> None:
        if not isinstance(reusable, bool):
            raise TypeError("reusable must be a boolean")
        if self._released:
            raise RuntimeError("connection lease has already been released")
        self._released = True
        self._pool._release(self._connection, reusable)

    def __enter__(self) -> ConnectionLease:
        if self._released:
            raise RuntimeError("connection lease has already been released")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if not self._released:
            self.release(self._connection.reusable)
