"""Tier-3 grader: an LLM judge for the two questions code cannot settle (docs/plans/llm-judge.md).

1. `judge_supported`: is every claim in the answer backed by the quotes it cites? The judge sees
   the question, the answer and the citations, never the gold answer.
2. `judge_responsive`: does the answer answer the question as asked? The judge sees the gold.

The judge never changes a grade code already decided. It skips errored and abstained attempts,
and skips "supported" when there are no citations, because `uncited_answer` has that covered.
Every call goes through `LLMClient`, so it is cached, timed and counted like the agent's calls;
the cache salt is part of the judge so a re-sample (stability probe) is one flag away.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_evals.config import JudgeConfig
from agent_evals.graders.judge_prompts import (
    JUDGE_RESPONSIVE_V1,
    JUDGE_SUPPORTED_V1,
    responsive_message,
    supported_message,
    verdict_tool,
)
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.llm import LLMClient
from agent_evals.types import AgentResult, Case, Grade, Usage


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pass", "fail"]
    reason: str


class JudgeGrader:
    name = "judge"

    def __init__(self, llm: LLMClient, config: JudgeConfig, *, salt: str = "judge") -> None:
        self.llm = llm
        self.config = config
        self.salt = salt
        self.tool = verdict_tool(config.strict_tools)
        self.usage = Usage()  # every judge call, for the cost line
        self._lock = threading.Lock()
        # Changes with the model, any prompt word, or the tool schema. Not with pricing or salt.
        fingerprint = {
            "grader": self.name,
            "model": config.model.model_dump(mode="json", exclude={"pricing"}),
            "prompts": {"supported": JUDGE_SUPPORTED_V1, "responsive": JUDGE_RESPONSIVE_V1},
            "tool": self.tool,
        }
        self.version = sha256_text(canonical_json(fingerprint))[:12]

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error or bool(result.output.get("abstained", False)):
            return []
        answer = str(result.output.get("answer", ""))
        raw = result.output.get("citations")
        citations = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        grades: list[Grade] = []
        if citations:
            v = self._ask(
                JUDGE_SUPPORTED_V1,
                supported_message(case.question, answer, citations),
                f"{self.salt}#supported#{case.id}",
            )
            grades.append(_grade("judge_supported", v))
        v = self._ask(
            JUDGE_RESPONSIVE_V1,
            responsive_message(case.question, case.gold.answer, answer),
            f"{self.salt}#responsive#{case.id}",
        )
        grades.append(_grade("judge_responsive", v))
        return grades

    def _ask(self, system: str, message: str, salt: str) -> Verdict:
        cfg = self.config.model
        request: dict[str, Any] = {
            "model": cfg.name,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "tools": [self.tool],
            "tool_choice": {"type": "tool", "name": self.tool["name"]},
            "messages": [{"role": "user", "content": message}],
        }
        if cfg.temperature is not None:
            request["temperature"] = cfg.temperature
        resp = self.llm.create(request, salt=salt)
        with self._lock:
            self.usage = self.usage.add(
                Usage(
                    input_tokens=int(resp.usage.get("input_tokens", 0)),
                    output_tokens=int(resp.usage.get("output_tokens", 0)),
                    model_calls=1,
                    cached_calls=1 if resp.cache_hit else 0,
                    latency_ms=resp.latency_ms,
                )
            )
        for block in resp.content:
            if block.get("type") == "tool_use" and block.get("name") == self.tool["name"]:
                try:
                    return Verdict.model_validate(block.get("input", {}))
                except ValidationError as exc:
                    raise ValueError(f"judge verdict failed validation: {exc}") from exc
        raise ValueError(f"judge returned no verdict (stop_reason={resp.stop_reason!r})")


def _grade(metric: str, v: Verdict) -> Grade:
    ok = v.verdict == "pass"
    return Grade(metric=metric, value=float(ok), passed=ok, reason=v.reason.strip())
