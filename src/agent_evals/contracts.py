"""Output contracts: the shape each agent's final output must have.

The agent that produces an output and the grader that checks it share these types, so neither has
to import the other (ADR 0011). A new agent adds one line to `OUTPUT_CONTRACTS`. The docs agent's
`submit_answer` tool spec states the same shape for the model, and a test keeps the two in step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    quote: str


class DocsAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    citations: list[Citation]
    abstained: bool
    confidence: float = Field(ge=0.0, le=1.0)


class CodeAnswer(BaseModel):
    """The code agent's answer: a module defining solve(session), or an abstention."""

    model_config = ConfigDict(extra="forbid")
    code: str
    abstained: bool
    explanation: str


# Keyed by `Case.agent`. Baselines answer through their agent's tool, so they share its contract.
OUTPUT_CONTRACTS: dict[str, type[BaseModel]] = {"docs": DocsAnswer, "code": CodeAnswer}


def contract_problems(agent: str, output: Mapping[str, Any]) -> list[str]:
    """What is wrong with an agent's final output. An empty list means it meets the contract.

    Strict, so nothing is coerced: the string "no" is not a bool, and would read as true. An agent
    with no contract is a problem too, so a new agent cannot pass this check by being unknown.
    """
    contract = OUTPUT_CONTRACTS.get(agent)
    if contract is None:
        return [f"no output contract for agent {agent!r}"]
    try:
        contract.model_validate(output, strict=True)
    except ValidationError as exc:
        return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return []
