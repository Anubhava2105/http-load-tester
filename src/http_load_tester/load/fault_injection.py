"""Explicit, deterministic client-side fault-injection policies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import random

from ..domain.models import HttpRequest


class FaultMode(StrEnum):
    NONE = "none"
    RAMP_UP = "ramp_up"
    TRAFFIC_SPIKE = "traffic_spike"
    CONNECTION_CHURN = "connection_churn"
    SLOW_REQUEST_BODY = "slow_request_body"
    ABORT_AFTER_HEADERS = "abort_after_headers"
    ABORT_DURING_RESPONSE = "abort_during_response"
    SHORT_READ_TIMEOUT = "short_read_timeout"
    RANDOMIZED_BODY = "randomized_body"


@dataclass(frozen=True, slots=True)
class FaultPolicy:
    mode: FaultMode
    probability: float = 1.0
    delay_seconds: float = 0.1
    randomized_body_bytes: int | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FaultMode):
            raise ValueError("mode must be a FaultMode")
        if isinstance(self.probability, bool) or not isinstance(self.probability, (int, float)):
            raise ValueError("probability must be numeric")
        if not 0 <= float(self.probability) <= 1:
            raise ValueError("probability must be between 0 and 1")
        if isinstance(self.delay_seconds, bool) or not isinstance(
            self.delay_seconds, (int, float)
        ) or self.delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if self.randomized_body_bytes is not None and (
            isinstance(self.randomized_body_bytes, bool)
            or not isinstance(self.randomized_body_bytes, int)
            or self.randomized_body_bytes < 0
        ):
            raise ValueError("randomized_body_bytes must be non-negative or None")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")


@dataclass(frozen=True, slots=True)
class FaultDecision:
    request: HttpRequest
    delay_seconds: float = 0.0
    force_connection_close: bool = False
    abort_after_headers: bool = False
    abort_during_response: bool = False
    read_timeout_seconds: float | None = None
    applied: bool = False


class FaultInjector:
    """Make deterministic per-attempt decisions without hidden retries."""

    def __init__(self, policy: FaultPolicy | None = None) -> None:
        if policy is not None and not isinstance(policy, FaultPolicy):
            raise TypeError("policy must be a FaultPolicy or None")
        self._policy = policy or FaultPolicy(FaultMode.NONE)

    @property
    def policy(self) -> FaultPolicy:
        return self._policy

    def apply(self, request: HttpRequest, sequence: int) -> FaultDecision:
        if not isinstance(request, HttpRequest):
            raise TypeError("request must be an HttpRequest")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        policy = self._policy
        if policy.mode is FaultMode.NONE:
            return FaultDecision(request)
        randomizer = random.Random(policy.seed + sequence)
        if randomizer.random() > float(policy.probability):
            return FaultDecision(request)
        if policy.mode is FaultMode.CONNECTION_CHURN:
            return FaultDecision(_with_connection_close(request), force_connection_close=True, applied=True)
        if policy.mode is FaultMode.SLOW_REQUEST_BODY:
            return FaultDecision(request, delay_seconds=float(policy.delay_seconds), applied=True)
        if policy.mode is FaultMode.ABORT_AFTER_HEADERS:
            return FaultDecision(request, abort_after_headers=True, applied=True)
        if policy.mode is FaultMode.ABORT_DURING_RESPONSE:
            return FaultDecision(request, abort_during_response=True, applied=True)
        if policy.mode is FaultMode.SHORT_READ_TIMEOUT:
            return FaultDecision(request, read_timeout_seconds=float(policy.delay_seconds), applied=True)
        if policy.mode is FaultMode.RANDOMIZED_BODY:
            size = len(request.body) if policy.randomized_body_bytes is None else policy.randomized_body_bytes
            body = bytes(randomizer.randrange(256) for _ in range(size))
            return FaultDecision(_with_body(request, body), applied=True)
        return FaultDecision(request, applied=True)


def _with_connection_close(request: HttpRequest) -> HttpRequest:
    headers = tuple((name, value) for name, value in request.headers if name.lower() != "connection")
    return replace(request, headers=headers + (("Connection", "close"),))


def _with_body(request: HttpRequest, body: bytes) -> HttpRequest:
    headers = tuple(
        (name, value)
        for name, value in request.headers
        if name.lower() not in {"content-length", "transfer-encoding"}
    )
    return replace(request, headers=headers, body=body)
