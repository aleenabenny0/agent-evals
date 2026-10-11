"""Core data types shared by the harness, agents, graders and store.

The harness only ever talks to an agent through the `Agent` protocol, so adding a new
agent means writing one adapter, not changing the harness.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_evals.config import Pricing

CaseType = Literal["lookup", "multi_step", "changed_default", "unanswerable", "false_premise"]
# Service area, for the case mix and per-area reporting (docs/plans/phase-2-measurement.md).
Area = Literal[
    "ai_ml",
    "compute",
    "storage",
    "databases",
    "networking",
    "security",
    "analytics",
    "integration",
    "management",
    "devtools",
    "migration",
]
# "unassigned" is for drafts: `evals split` assigns dev/test, and runs refuse unassigned cases.
Split = Literal["smoke", "dev", "test", "unassigned"]


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    # Each inner list is a group of acceptable alternatives; every group must match.
    # Example: [["50 TB", "50TB"], ["5 GB", "5GB"]]
    must_include: list[list[str]] = Field(default_factory=list)
    # Pages that support the gold answer, as groups of alternatives: every group must be read
    # or cited, and any URL inside a group satisfies it. A flat list is shorthand for one group
    # per URL (all required). The same fact often sits on several pages, and the agent must not
    # be penalized for grounding on one of them rather than another (ADR 0006).
    evidence: list[list[str]] = Field(default_factory=list)
    # The sentences on the evidence pages that the answer rests on, copied word for word, one per
    # fact the answer uses. A reviewer verifies a draft by reading them in place, and `evals
    # validate` proves each exists on a page and warns when a required fact has no quote. A
    # single string is accepted as shorthand for a one-item list.
    support: list[str] = Field(default_factory=list)

    @field_validator("support", mode="before")
    @classmethod
    def _wrap_single_support(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("evidence", mode="before")
    @classmethod
    def _wrap_flat_evidence(cls, value: object) -> object:
        if isinstance(value, list) and all(isinstance(u, str) for u in value):
            return [[u] for u in value]
        return value

    @property
    def evidence_urls(self) -> list[str]:
        return [url for group in self.evidence for url in group]


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    suite: Literal["smoke", "capability", "robustness", "red_team"]
    agent: str
    type: CaseType
    question: str
    gold: Gold
    split: Split
    provenance: Literal["hand", "synthetic"]
    area: Area | None = None
    # Who checked this case against its page. A synthetic case without it is a draft and is
    # never scored by accident (see `evals run --allow-drafts`).
    verified_by: str | None = None
    # Why this case must stay out of the test split for now, e.g. keyword grading cannot tell
    # the right recommendation from the wrong one until the judge exists. `evals split` puts
    # held cases in dev. Clear the field when the reason no longer applies.
    hold: str | None = None
    # For robustness cases: the id of the source case this question paraphrases. Paraphrases
    # share the source's gold and live in the dev split; consistency is measured per group.
    paraphrase_of: str | None = None
    notes: str | None = None

    @property
    def answerable(self) -> bool:
        return self.type != "unanswerable"

    @property
    def is_draft(self) -> bool:
        return self.provenance == "synthetic" and not self.verified_by


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
