"""Core data types shared by the harness, agents, graders and store.

The harness only ever talks to an agent through the `Agent` protocol, so adding a new
agent means writing one adapter, not changing the harness.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
# "online" is simulated traffic (phase 7), which belongs to no offline split.
Split = Literal["smoke", "dev", "test", "unassigned", "online"]


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
    suite: Literal["smoke", "capability", "robustness", "red_team", "online"]
    agent: str
    type: CaseType
    question: str
    # Absent only on an online case (simulated traffic has no reference answer); there it may
    # hold at most a minimal key, a false premise's contradicted sentence as `support`.
    gold: Gold | None = None
    split: Split
    # "simulated" is phase 7 traffic: generated, never verified by hand, and not a draft.
    provenance: Literal["hand", "synthetic", "simulated"]
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

    @model_validator(mode="after")
    def _online_rules(self) -> Case:
        """Suite, split and provenance mark an online case together or not at all. An offline
        case must have gold. An online case's gold, when present, holds only `support`: an empty
        gold, or one with an answer, phrases or evidence, could score it correct by accident."""
        marks = {self.suite == "online", self.split == "online", self.provenance == "simulated"}
        if len(marks) != 1:
            raise ValueError(
                "an online case has suite 'online', split 'online' and provenance 'simulated'; "
                "no other case has any of them"
            )
        if not self.is_online:
            if self.gold is None:
                raise ValueError("gold may be absent only on an online case")
        elif self.gold is not None:
            g = self.gold
            if g.answer.strip() or g.must_include or g.evidence:
                raise ValueError(
                    "an online case's gold may hold only `support`, never an answer, phrases "
                    "or evidence"
                )
            if not g.support or not all(s.strip() for s in g.support):
                raise ValueError("an online case with an empty gold is rejected; leave it out")
            if self.type != "false_premise":
                raise ValueError("only an online false premise has a gold: its minimal key")
        return self

    @property
    def is_online(self) -> bool:
        return self.suite == "online"

    @property
    def answerable(self) -> bool:
        return self.type != "unanswerable"

    @property
    def is_draft(self) -> bool:
        return self.provenance == "synthetic" and not self.verified_by


class Usage(BaseModel):
    input_tokens: int = 0  # the whole prompt, however much of it the prompt cache served
    output_tokens: int = 0
    # Of input_tokens, the part the API's prompt cache wrote or read, which is billed at its own
    # multiple of the input price. Not to be confused with the local call cache below.
    prompt_cache_write_tokens: int = 0
    prompt_cache_read_tokens: int = 0
    model_calls: int = 0
    cached_calls: int = 0  # served from the local call cache: no API spend
    latency_ms: float = 0.0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            prompt_cache_write_tokens=self.prompt_cache_write_tokens
            + other.prompt_cache_write_tokens,
            prompt_cache_read_tokens=self.prompt_cache_read_tokens + other.prompt_cache_read_tokens,
            model_calls=self.model_calls + other.model_calls,
            cached_calls=self.cached_calls + other.cached_calls,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    def cost_usd(self, pricing: Pricing) -> float | None:
        """Nominal cost of the tokens, whether or not the local call cache served them. Prompt
        cache writes and reads are billed at their own multiples of the input price."""
        if pricing.input_per_mtok is None or pricing.output_per_mtok is None:
            return None
        write, read = self.prompt_cache_write_tokens, self.prompt_cache_read_tokens
        prompt = (
            self.input_tokens
            - write
            - read
            + write * pricing.cache_write_multiplier
            + read * pricing.cache_read_multiplier
        )
        return (prompt * pricing.input_per_mtok + self.output_tokens * pricing.output_per_mtok) / (
            1_000_000
        )


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
