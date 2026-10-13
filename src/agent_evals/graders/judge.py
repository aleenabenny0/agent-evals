"""Tier-3 grader: an LLM judge for the questions code cannot settle (docs/plans/llm-judge.md).

1. `judge_core_supported`: are the claims that answer the question backed by the quotes the
   answer cites? 2. `judge_supported`: is every claim, including added context, backed by them?
   Both come from one call that sees the question, the answer and the citations, never the gold.
3. `judge_responsive`: does the answer answer the question as asked? This call sees the gold.

The judge never changes a grade code already decided. It skips errored and abstained attempts,
and skips the support verdicts when there are no citations, because `uncited_answer` has that
covered. Every call goes through `LLMClient`, so it is cached, timed and counted like the
agent's calls; the cache salt is part of the judge so a re-sample (stability probe) is one flag
away.
"""

from __future__ import annotations

import threading
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_evals.config import JudgeConfig
from agent_evals.graders.judge_prompts import (
    JUDGE_RESPONSIVE_V1,
    JUDGE_SUPPORTED_V2,
    responsive_message,
    support_verdict_tool,
    supported_message,
    verdict_tool,
)
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.llm import LLMClient
from agent_evals.types import AgentResult, Case, Grade, Usage

PassFail = Literal["pass", "fail"]


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: PassFail
    reason: str


class SupportVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    core_supported: PassFail
    fully_supported: PassFail
    reason: str


V = TypeVar("V", bound=BaseModel)


class JudgeGrader:
    name = "judge"

    def __init__(self, llm: LLMClient, config: JudgeConfig, *, salt: str = "judge") -> None:
        self.llm = llm
        self.config = config
        self.salt = salt
        self.tool = verdict_tool(config.strict_tools)
        self.support_tool = support_verdict_tool(config.strict_tools)
        self.usage = Usage()  # every judge call, for the cost line
        self._lock = threading.Lock()
        # Changes with the model, any prompt word, or a tool schema. Not with pricing or salt.
        fingerprint = {
            "grader": self.name,
            "model": config.model.model_dump(mode="json", exclude={"pricing"}),
            "prompts": {"supported": JUDGE_SUPPORTED_V2, "responsive": JUDGE_RESPONSIVE_V1},
            "tools": [self.support_tool, self.tool],
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
            s = self._ask(
                JUDGE_SUPPORTED_V2,
                supported_message(case.question, answer, citations),
                f"{self.salt}#supported#{case.id}",
                self.support_tool,
                SupportVerdict,
            )
            # A fully supported answer has supported core claims by definition; never let the
            # two verdicts contradict each other.
            core_ok = s.core_supported == "pass" or s.fully_supported == "pass"
            full_ok = s.fully_supported == "pass"
            grades.append(_grade("judge_core_supported", core_ok, s.reason))
            grades.append(_grade("judge_supported", full_ok, s.reason))
        r = self._ask(
            JUDGE_RESPONSIVE_V1,
            responsive_message(case.question, case.gold.answer, answer),
            f"{self.salt}#responsive#{case.id}",
            self.tool,
            Verdict,
        )
        grades.append(_grade("judge_responsive", r.verdict == "pass", r.reason))
        return grades

    def _ask(self, system: str, message: str, salt: str, tool: dict[str, Any], model: type[V]) -> V:
        cfg = self.config.model
        # No forced tool_choice: claude-sonnet-5-5 rejects `{"type": "tool"}` (400). The system
        # prompt asks for the tool, and a reply without the call is a grader_error, never a pass.
        request: dict[str, Any] = {
            "model": cfg.name,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "tools": [tool],
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
        if resp.stop_reason == "max_tokens":
            # A truncated tool input parses as a partial dict; never grade from one.
            raise ValueError(
                f"judge hit max_tokens={cfg.max_tokens} before finishing its verdict; raise "
                "judge.model.max_tokens in the config"
            )
        for block in resp.content:
            if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
                try:
                    return model.model_validate(block.get("input", {}))
                except ValidationError as exc:
                    raise ValueError(f"judge verdict failed validation: {exc}") from exc
        raise ValueError(f"judge returned no verdict (stop_reason={resp.stop_reason!r})")


def _grade(metric: str, ok: bool, reason: str) -> Grade:
    return Grade(metric=metric, value=float(ok), passed=ok, reason=reason.strip())
