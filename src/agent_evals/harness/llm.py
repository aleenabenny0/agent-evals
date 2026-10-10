"""Thin, cached wrapper around the Claude Messages API.

Agents depend on the `LLMClient` protocol, not on the SDK, so tests can swap in a scripted
fake and every agent gets caching and timing for free.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agent_evals.harness.cache import CallCache


class LLMResponse(BaseModel):
    content: list[dict[str, Any]]
    stop_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    cache_hit: bool = False
    latency_ms: float = 0.0


class LLMClient(Protocol):
    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse: ...


class AnthropicClient:
    """Calls `messages.create`, consulting the call cache first."""

    def __init__(self, cache: CallCache | None = None, max_retries: int = 5) -> None:
        self._cache = cache
        self._max_retries = max_retries
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            import anthropic  # imported lazily so offline commands never need an API key

            self._client = anthropic.Anthropic(max_retries=self._max_retries)
        return self._client

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        key = CallCache.key(request, salt)
        if self._cache is not None and (hit := self._cache.get(key)) is not None:
            return LLMResponse.model_validate({**hit, "cache_hit": True, "latency_ms": 0.0})

        start = time.perf_counter()
        response = self._sdk().messages.create(**request)
        latency_ms = (time.perf_counter() - start) * 1000

        data = response.model_dump(mode="json", exclude_none=True)
        payload = {
            "content": data.get("content", []),
            "stop_reason": data.get("stop_reason"),
            "usage": data.get("usage", {}),
            "model": data.get("model", ""),
        }
        if self._cache is not None:
            self._cache.put(key, payload)
        return LLMResponse.model_validate({**payload, "latency_ms": latency_ms})
