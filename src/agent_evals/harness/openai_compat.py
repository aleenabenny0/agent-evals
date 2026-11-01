"""An `LLMClient` for any provider that speaks the OpenAI chat completions API.

Requests stay in the shape the agent already uses, the Anthropic one, so the agent never knows
which provider answered. This client translates each call both ways: the tools, the conversation
so far with its tool calls and tool results, and the reply. A conversation can therefore start on
one provider and finish on another, which is what failover per call needs (`harness/gateway.py`).

Strict tool schemas are not sent, since providers differ in what they accept, and the agent
validates every tool input itself either way. Thinking blocks have no counterpart and are dropped.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from agent_evals.harness.cache import CallCache
from agent_evals.harness.llm import LLMResponse

STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class ProviderHTTPError(RuntimeError):
    """A provider answered with an error status. The gateway reads `status_code`."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


def _tool(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": spec["input_schema"],
        },
    }


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        return "\n".join(p for p in parts if p)
    return ""


def _user(content: Any) -> list[dict[str, Any]]:
    """A user turn: its tool results first, each a message of its own, then any text."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    found: list[dict[str, Any]] = []
    texts: list[str] = []
    for block in content:
        if block.get("type") == "tool_result":
            body = _text_of(block.get("content", ""))
            found.append(
                {
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": f"ERROR: {body}" if block.get("is_error") else body,
                }
            )
        elif block.get("type") == "text":
            texts.append(block.get("text", ""))
    if texts:
        found.append({"role": "user", "content": "\n".join(texts)})
    return found


def _assistant(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts = [b.get("text", "") for b in content if b.get("type") == "text"]
    calls = [
        {
            "id": b["id"],
            "type": "function",
            "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
        }
        for b in content
        if b.get("type") == "tool_use"
    ]
    turn: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
    if calls:
        turn["tool_calls"] = calls
    return turn


def to_openai(
    request: dict[str, Any], model: str, max_tokens_field: str = "max_completion_tokens"
) -> dict[str, Any]:
    """The request in the OpenAI shape, for this provider's model."""
    messages: list[dict[str, Any]] = []
    if request.get("system"):
        messages.append({"role": "system", "content": request["system"]})
    for turn in request["messages"]:
        if turn["role"] == "assistant":
            messages.append(_assistant(turn["content"]))
        else:
            messages += _user(turn["content"])
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        max_tokens_field: request["max_tokens"],
    }
    if request.get("tools"):
        body["tools"] = [_tool(spec) for spec in request["tools"]]
    if request.get("temperature") is not None:
        body["temperature"] = request["temperature"]
    return body


def _arguments(raw: str) -> dict[str, Any]:
    """A tool call's input. Bad JSON becomes an empty input, which the agent's own validation
    sends back to the model as a tool error, as it would any wrong input."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def from_openai(data: dict[str, Any]) -> dict[str, Any]:
    """The reply in the shape `LLMResponse` holds."""
    choice = data["choices"][0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": function.get("name", ""),
                "input": _arguments(function.get("arguments") or "{}"),
            }
        )
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return {
        "content": content,
        "stop_reason": STOP_REASONS.get(choice.get("finish_reason") or "", "end_turn"),
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0) - cached,
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_input_tokens": cached,
        },
        "model": str(data.get("model", "")),
    }


class OpenAICompatibleClient:
    """Calls `{base_url}/chat/completions`, consulting the call cache first when it has one."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        max_tokens_field: str = "max_completion_tokens",
        cache: CallCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.max_tokens_field = max_tokens_field
        self._cache = cache
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        body = to_openai(request, self.model, self.max_tokens_field)
        key = CallCache.key(body, salt)
        if self._cache is not None and (hit := self._cache.get(key)) is not None:
            return LLMResponse.model_validate({**hit, "cache_hit": True, "latency_ms": 0.0})
        start = time.perf_counter()
        response = self._http.post("/chat/completions", json=body)
        latency_ms = (time.perf_counter() - start) * 1000
        if response.status_code >= 400:
            raise ProviderHTTPError(response.status_code, response.text[:200])
        payload = from_openai(response.json())
        if self._cache is not None:
            self._cache.put(key, payload)
        return LLMResponse.model_validate({**payload, "latency_ms": latency_ms})
