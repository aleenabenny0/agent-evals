"""The gateway: an `LLMClient` that survives a provider going down.

`ResilientClient` wraps other clients in order of preference, so nothing above it changes. Failover
happens per model call, not per answer: an answer three steps in continues on the next provider,
which is why the OpenAI-compatible client translates a whole conversation on every call.

Each provider has a circuit breaker. After repeated failures it stops sending to that provider for
a cool-down, then lets a single request through to test it. Without one, every request in an outage
would first wait out a full timeout on the provider that is down.

Only a failure of the provider counts. A request the provider refuses as malformed is a bug here,
and the next provider would refuse it too, so it is raised at once and trips nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from agent_evals.harness.llm import LLMClient, LLMResponse, usage_from
from agent_evals.types import Usage

# HTTP statuses that mean the request was wrong, not that the provider failed.
CALLER_ERRORS = frozenset({400, 404, 413, 422})

BreakerState = Literal["closed", "open", "half_open"]
EventKind = Literal["failure", "failover", "circuit_open", "circuit_closed"]


class AllProvidersFailed(RuntimeError):
    """No provider could take this call."""


def is_caller_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in CALLER_ERRORS


class CircuitBreaker:
    """Closed lets calls through. After `failure_threshold` failures in a row it opens and lets
    none through for `cooldown_s`. Then it is half open: one call may test the provider, which
    closes it on success and reopens it on failure."""

    def __init__(
        self,
        failure_threshold: int,
        cooldown_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self._opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        """May a call go through now? In half open, only the first caller gets a yes."""
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "half_open" and not self._probing:
                self._probing = True
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probing = 0, None, False

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._probing or self._failures >= self.failure_threshold:
                self._opened_at = self.clock()
            self._probing = False


@dataclass(frozen=True)
class Provider:
    name: str
    client: LLMClient
    breaker: CircuitBreaker


@dataclass(frozen=True)
class CallRecord:
    """One model call as a provider served it, for what the answer really cost."""

    provider: str
    usage: Usage


@dataclass(frozen=True)
class GatewayEvent:
    kind: EventKind
    provider: str
    detail: str = ""


# The calls of the answer in progress. Set by whoever wants them, read by nobody else.
CURRENT_CALLS: ContextVar[list[CallRecord] | None] = ContextVar("current_calls", default=None)


def _nothing(_: GatewayEvent) -> None:
    return None


class ResilientClient:
    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        on_event: Callable[[GatewayEvent], None] = _nothing,
    ) -> None:
        if not providers:
            raise ValueError("a gateway needs at least one provider")
        self.providers = list(providers)
        self.on_event = on_event

    def available(self) -> bool:
        """At least one provider could take a call now, or could be tested."""
        return any(p.breaker.state != "open" for p in self.providers)

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        problems: list[str] = []
        for n, provider in enumerate(self.providers):
            if not provider.breaker.allow():
                problems.append(f"{provider.name}: circuit open")
                continue
            before = provider.breaker.state
            try:
                resp = provider.client.create(request, salt=salt)
            except Exception as exc:
                if is_caller_error(exc):
                    provider.breaker.success()  # it answered; the request was the problem
                    raise
                kind = type(exc).__name__
                problems.append(f"{provider.name}: {kind}")
                provider.breaker.failure()
                self.on_event(GatewayEvent("failure", provider.name, kind))
                if provider.breaker.state != "closed":
                    self.on_event(GatewayEvent("circuit_open", provider.name))
                continue
            provider.breaker.success()
            if before != "closed":
                self.on_event(GatewayEvent("circuit_closed", provider.name))
            if n > 0:
                self.on_event(GatewayEvent("failover", provider.name))
            calls = CURRENT_CALLS.get()
            if calls is not None:
                calls.append(CallRecord(provider.name, usage_from(resp)))
            return resp
        raise AllProvidersFailed("; ".join(problems))
