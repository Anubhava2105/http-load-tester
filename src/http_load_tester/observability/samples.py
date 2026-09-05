"""Thread-safe ingestion of validated attempt samples."""

from __future__ import annotations

from queue import Full, Empty, Queue
import threading

from ..domain.models import ResultSample


class SampleCollector:
    """Accept samples from workers and drain them in an owner thread."""

    def __init__(self, *, max_samples: int | None = None) -> None:
        if max_samples is not None and (
            isinstance(max_samples, bool)
            or not isinstance(max_samples, int)
            or max_samples <= 0
        ):
            raise ValueError("max_samples must be a positive integer or None")
        self._queue: Queue[ResultSample] = Queue(maxsize=max_samples or 0)
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def submit(self, sample: ResultSample) -> None:
        """Queue one sample without blocking a load worker."""
        if not isinstance(sample, ResultSample):
            raise TypeError("sample must be a ResultSample")
        with self._lock:
            if self._closed:
                raise RuntimeError("sample collector is closed")
            try:
                self._queue.put_nowait(sample)
            except Full as exc:
                raise OverflowError("sample collector capacity is exhausted") from exc

    def collect(self, *, limit: int | None = None) -> tuple[ResultSample, ...]:
        """Drain currently queued samples in FIFO order."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None")
        collected: list[ResultSample] = []
        while limit is None or len(collected) < limit:
            try:
                collected.append(self._queue.get_nowait())
            except Empty:
                break
        return tuple(collected)

    def close(self) -> None:
        """Reject future submissions while preserving queued samples."""
        with self._lock:
            self._closed = True
