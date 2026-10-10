"""Core data types shared by the harness, agents, graders and store.

The harness only ever talks to an agent through the `Agent` protocol, so adding a new
agent means writing one adapter, not changing the harness.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agent_evals.config import Pricing

CaseType = Literal["lookup", "multi_step", "changed_default", "unanswerable", "false_premise"]


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    # Each inner list is a group of acceptable alternatives; every group must match.
    # Example: [["50 TB", "50TB"], ["5 GB", "5GB"]]
    must_include: list[list[str]] = Field(default_factory=list)
    # URLs of the pages that support the gold answer.
    evidence: list[str] = Field(default_factory=list)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    suite: Literal["smoke", "capability", "robustness", "red_team"]
    agent: str
    type: CaseType
    question: str
    gold: Gold
    split: Literal["smoke", "dev", "test"]
    provenance: Literal["hand", "synthetic"]
    notes: str | None = None

    @property
    def answerable(self) -> bool:
        return self.type != "unanswerable"


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    cached_calls: int = 0  # served from the local call cache: no API spend
    latency_ms: float = 0.0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model_calls=self.model_calls + other.model_calls,
            cached_calls=self.cached_calls + other.cached_calls,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    def cost_usd(self, pricing: Pricing) -> float | None:
        """Nominal cost of the tokens, whether or not they were served from cache."""
        if pricing.input_per_mtok is None or pricing.output_per_mtok is None:
            return None
        return (
            self.input_tokens * pricing.input_per_mtok
            + self.output_tokens * pricing.output_per_mtok
        ) / 1_000_000


class Step(BaseModel):
    """One entry in an agent's trajectory: a model call or a tool call."""

    index: int
    kind: Literal["model", "tool"]
    name: str
    input: Any = None
    output: Any = None
    is_error: bool = False
    latency_ms: float = 0.0
    cache_hit: bool | None = None


class AgentResult(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)
    trajectory: list[Step] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None


class RunContext(BaseModel):
    run_id: str
    case_id: str
    attempt: int

    @property
    def cache_salt(self) -> str:
        # Separate attempts must be separate samples, but each must be reproducible.
        return f"{self.case_id}#{self.attempt}"


@runtime_checkable
class Agent(Protocol):
    name: str

    @property
    def version(self) -> str: ...

    def describe(self) -> dict[str, Any]: ...

    def run(self, case: Case, ctx: RunContext) -> AgentResult: ...


class Grade(BaseModel):
    metric: str
    value: float
    passed: bool | None = None
    reason: str = ""


class Grader(Protocol):
    name: str
    version: str

    def grade(self, case: Case, result: AgentResult) -> list[Grade]: ...
