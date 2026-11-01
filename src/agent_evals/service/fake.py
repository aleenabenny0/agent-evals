"""A fake provider, so the service can be load-tested for free.

It plays the docs agent's loop the way the model does: search, read the top page, submit its first
sentence as a cited answer. The tools, the corpus search and the submission checks all really run,
so the service does its real work and only the model is replaced. Each reply sleeps for a random
time around a set latency, since waiting on the model is what fills the service up. It says what
it is: every answer reports the provider `fake`.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from typing import Any

from agent_evals.harness.llm import LLMResponse

_URL = re.compile(r"^URL: (\S+)$", re.MULTILINE)
_SENTENCE = re.compile(r"[A-Z][^\n.|#*`\[\]]{30,200}\.")


def _last_tool_result(request: dict[str, Any]) -> str:
    content = request["messages"][-1]["content"]
    if isinstance(content, list) and content:
        return str(content[0].get("content", ""))
    return ""


def _tool_use(name: str, inputs: dict[str, Any], step: int) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"fake_{step}", "name": name, "input": inputs}


class FakeProvider:
    """An `LLMClient`. Latency is lognormal around `latency_s`, for a realistic slow tail."""

    def __init__(
        self,
        latency_s: float,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.latency_s = latency_s
        self.rng = rng or random.Random()
        self.sleep = sleep

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        delay = self.latency_s * self.rng.lognormvariate(0.0, 0.5)
        self.sleep(delay)
        return LLMResponse(
            content=[self._next_step(request)],
            stop_reason="tool_use",
            usage={"input_tokens": 1500, "output_tokens": 120},
            model="fake",
            latency_ms=delay * 1000,
        )

    def _next_step(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request["messages"]
        step = sum(turn["role"] == "assistant" for turn in messages)
        if step == 0:
            question = str(messages[0]["content"])
            return _tool_use("search_docs", {"query": question[:80]}, step)
        result = _last_tool_result(request)
        if step == 1:
            found = _URL.search(result)
            if found is None:
                return self._abstain(step)
            return _tool_use("read_page", {"url": found.group(1), "start": 0}, step)
        previous = next((b for b in messages[-2]["content"] if b.get("type") == "tool_use"), None)
        url = str(previous["input"].get("url", "")) if previous else ""
        sentence = _SENTENCE.search(result)
        if not url or sentence is None:
            return self._abstain(step)
        quote = sentence.group(0)
        answer = {
            "answer": quote,
            "citations": [{"url": url, "quote": quote}],
            "abstained": False,
            "confidence": 0.5,
        }
        return _tool_use("submit_answer", answer, step)

    def _abstain(self, step: int) -> dict[str, Any]:
        answer = {"answer": "", "citations": [], "abstained": True, "confidence": 0.0}
        return _tool_use("submit_answer", answer, step)
