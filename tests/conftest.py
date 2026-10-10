from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from agent_evals.agents.docs.corpus import Corpus, Page
from agent_evals.harness.cache import sha256_text
from agent_evals.harness.llm import LLMResponse
from agent_evals.types import Case, Gold

BUCKETS_URL = "https://docs.example.test/s3/bucket-naming.html"
LAMBDA_URL = "https://docs.example.test/lambda/quotas.html"


def make_page(url: str, title: str, text: str) -> Page:
    return Page(
        url=url,
        title=title,
        text=text,
        fetched_at="2026-10-10T00:00:00+00:00",
        sha256=sha256_text(text),
    )


@pytest.fixture
def corpus() -> Corpus:
    # Synthetic pages: tests must never depend on the live docs or a fetched snapshot.
    return Corpus(
        [
            make_page(
                BUCKETS_URL,
                "Bucket naming rules",
                (
                    "# Bucket naming rules\n\n"
                    "Bucket names must be between **3** and **63** characters long.\n\n"
                    "Bucket names can contain only lowercase letters, numbers, dots, hyphens.\n\n"
                    "## Examples\n\nThe name `my-example-bucket` is valid."
                ),
            ),
            make_page(
                LAMBDA_URL,
                "Lambda quotas",
                (
                    "# Lambda quotas\n\n"
                    "| Resource | Quota |\n|---|---|\n"
                    "| Function timeout | 900 seconds (15 minutes) |\n\n"
                    "## Memory\n\nFunctions can use between 128 MB and 10,240 MB of memory."
                ),
            ),
        ]
    )


def make_case(**overrides: Any) -> Case:
    base: dict[str, Any] = {
        "id": "t-1",
        "suite": "smoke",
        "agent": "docs",
        "type": "lookup",
        "question": "How long can a bucket name be?",
        "gold": Gold(answer="3 to 63 characters", must_include=[["63"]], evidence=[BUCKETS_URL]),
        "split": "smoke",
        "provenance": "hand",
    }
    base.update(overrides)
    return Case(**base)


@pytest.fixture
def case() -> Case:
    return make_case()


def tool_use(name: str, inp: dict[str, Any], id_: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


class ScriptedLLM:
    """Replays a fixed list of responses and records every request it receives."""

    def __init__(self, script: list[list[dict[str, Any]]], stop_reason: str = "tool_use") -> None:
        self.script = list(script)
        self.stop_reason = stop_reason
        self.requests: list[dict[str, Any]] = []

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        self.requests.append(copy.deepcopy(request))  # the agent keeps mutating its list
        content = self.script.pop(0)
        has_tool = any(b["type"] == "tool_use" for b in content)
        return LLMResponse(
            content=content,
            stop_reason=self.stop_reason if has_tool else "end_turn",
            usage={"input_tokens": 100, "output_tokens": 20},
            model="fake",
            latency_ms=1.0,
        )


@pytest.fixture
def scripted() -> Callable[..., ScriptedLLM]:
    return ScriptedLLM
