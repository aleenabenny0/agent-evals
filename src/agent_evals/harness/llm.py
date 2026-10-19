"""Thin, cached wrapper around the Claude Messages API.

Agents depend on the `LLMClient` protocol, not on the SDK, so tests can swap in a scripted
fake and every agent gets caching and timing for free.

Two caches, not to be confused. The local call cache stores whole responses, so a rerun costs
nothing. The API's prompt cache, asked for on every call sent, bills a repeated prompt prefix
(tools, system prompt, earlier turns) at a fraction of the input price, and the response is
the same as without it. The prompt cache is requested at send time, so it is in neither the
local cache key nor any agent or judge fingerprint: past calls still replay, versions hold.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agent_evals.harness.cache import CallCache
from agent_evals.types import Usage


class LLMResponse(BaseModel):
    content: list[dict[str, Any]]
    stop_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    cache_hit: bool = False
    latency_ms: float = 0.0


class LLMClient(Protocol):
    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse: ...


def usage_from(resp: LLMResponse) -> Usage:
    """One call's usage. The API leaves what the prompt cache wrote or read out of its own
    input_tokens; here input_tokens is the whole prompt, so token metrics stay comparable."""
    u = resp.usage
    write = int(u.get("cache_creation_input_tokens") or 0)
    read = int(u.get("cache_read_input_tokens") or 0)
    return Usage(
        input_tokens=int(u.get("input_tokens") or 0) + write + read,
        output_tokens=int(u.get("output_tokens") or 0),
        prompt_cache_write_tokens=write,
        prompt_cache_read_tokens=read,
        model_calls=1,
        cached_calls=1 if resp.cache_hit else 0,
        latency_ms=resp.latency_ms,
    )


def with_prompt_cache(request: dict[str, Any]) -> dict[str, Any]:
    """The request as sent, asking for the prompt cache. The system prompt, with the tools before
    it, is shared by every call of a run, so it gets a breakpoint of its own. A conversation of
    more than one message also gets automatic caching, which puts a breakpoint on its last block,
    so each turn of a tool loop reuses the turns before it. A one-message request (a judge or
    generator call) gets no more than that: its message is used once, and writing it to the
    cache would only add the 1.25x write price. The caller's request is not changed."""
    if "cache_control" in request:  # the caller manages the cache itself
        return request
    marker = {"type": "ephemeral"}
    sent = dict(request)
    used = _breakpoints(request)
    system = sent.get("system")
    if used < MAX_BREAKPOINTS:
        if isinstance(system, str) and system.strip():
            sent["system"] = [{"type": "text", "text": system, "cache_control": marker}]
            used += 1
        elif isinstance(system, list) and system and _markable(system[-1]):
            sent["system"] = [*system[:-1], {**system[-1], "cache_control": marker}]
            used += 1
    if len(sent.get("messages", [])) > 1 and used < MAX_BREAKPOINTS:
        sent["cache_control"] = marker
    return sent


MAX_BREAKPOINTS = 4  # the API's limit per request, automatic caching included


def _markable(block: Any) -> bool:
    """A system block that can take a breakpoint: not already marked (its TTL is kept), and not
    an empty text block, which the API cannot cache."""
    if not isinstance(block, dict) or "cache_control" in block:
        return False
    return block.get("type") != "text" or bool(str(block.get("text", "")).strip())


def _breakpoints(request: dict[str, Any]) -> int:
    """The cache_control markers a request already carries, in tools, system and messages."""
    blocks: list[Any] = list(request.get("tools") or [])
    if isinstance(request.get("system"), list):
        blocks += request["system"]
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            blocks += content
    return sum(1 for b in blocks if isinstance(b, dict) and "cache_control" in b)


class AnthropicClient:
    """Calls `messages.create`, consulting the call cache first; every call sent asks for the
    prompt cache (`with_prompt_cache`)."""

    def __init__(
        self, cache: CallCache | None = None, max_retries: int = 5, prompt_cache: bool = True
    ) -> None:
        self._cache = cache
        self._max_retries = max_retries
        self._prompt_cache = prompt_cache
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

        sent = with_prompt_cache(request) if self._prompt_cache else request
        start = time.perf_counter()
        response = self._sdk().messages.create(**sent)
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
