"""Explicit, deterministic client-side fault-injection policies."""

from __future__ import annotations

from dataclasses import replace
import random

from ..domain.models import (
    FaultDecision,
    FaultMode,
    FaultPolicy,
    HttpRequest,
)


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
        mode_value = policy.mode.value
        if policy.mode is FaultMode.CONNECTION_CHURN:
            return FaultDecision(
                _with_connection_close(request),
                force_connection_close=True,
                applied=True,
                fault_mode=mode_value,
            )
        if policy.mode is FaultMode.SLOW_REQUEST_BODY:
            return FaultDecision(
                request,
                delay_seconds=float(policy.delay_seconds),
                applied=True,
                fault_mode=mode_value,
            )
        if policy.mode is FaultMode.ABORT_AFTER_HEADERS:
            return FaultDecision(
                request,
                abort_after_headers=True,
                applied=True,
                fault_mode=mode_value,
            )
        if policy.mode is FaultMode.ABORT_DURING_RESPONSE:
            return FaultDecision(
                request,
                abort_during_response=True,
                applied=True,
                fault_mode=mode_value,
            )
        if policy.mode is FaultMode.SHORT_READ_TIMEOUT:
            return FaultDecision(
                request,
                read_timeout_seconds=float(policy.delay_seconds),
                applied=True,
                fault_mode=mode_value,
            )
        if policy.mode is FaultMode.RANDOMIZED_BODY:
            size = (
                len(request.body)
                if policy.randomized_body_bytes is None
                else policy.randomized_body_bytes
            )
            body = bytes(randomizer.randrange(256) for _ in range(size))
            return FaultDecision(
                _with_body(request, body),
                applied=True,
                fault_mode=mode_value,
            )
        return FaultDecision(request, applied=True, fault_mode=mode_value)


def _with_connection_close(request: HttpRequest) -> HttpRequest:
    headers = tuple(
        (name, value)
        for name, value in request.headers
        if name.lower() != "connection"
    )
    return replace(request, headers=headers + (("Connection", "close"),))


def _with_body(request: HttpRequest, body: bytes) -> HttpRequest:
    headers = tuple(
        (name, value)
        for name, value in request.headers
        if name.lower() not in {"content-length", "transfer-encoding"}
    )
    return replace(request, headers=headers, body=body)
