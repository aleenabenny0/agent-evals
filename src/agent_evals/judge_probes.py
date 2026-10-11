"""Mutation tests and bias probes for the LLM judge (docs/plans/llm-judge.md, steps 2 to 4).

Agreement with hand labels is not enough on its own: on a mostly-correct run a judge that passes
everything also "agrees" most of the time. So two more checks, each built from answers the judge
passed on both verdicts:

- **Mutants** plant one known error in a passed answer. The judge must fail the mutant on at least
  one verdict. Three kinds: a changed number, a dropped qualifier ("by default", "up to"), and a
  citation whose quote is swapped for an unrelated one.
- **Probes** change nothing of substance. The judge must return the same verdicts. Two kinds:
  the answer padded with a harmless closing sentence (length bias), and the very same answer
  judged again under a different cache salt (stability).

Everything here is deterministic text surgery, so the same run always yields the same variants.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_evals.entities import NUMBER

MUTANTS = ("number", "qualifier", "citation")
PROBES = ("padding", "resample")
QUALIFIERS = (
    "by default",
    "per Region",
    "per AWS account",
    "per account",
    "at most",
    "up to",
    "at least",
    "only",
    "approximately",
    "about",
)
PADDING = " I hope this helps; let me know if you need anything else about this topic."


def mutate_number(answer: str) -> str | None:
    """Change the first standalone number to a clearly different one, keeping its format.

    Years move by one (2026 -> 2027), small counts by one (5 -> 6), everything else doubles
    (900 -> 1800, 0.5 -> 1). None when the answer has no number to change.
    """
    m = NUMBER.search(answer)
    if m is None:
        return None
    raw = m.group(1)
    num = raw.replace(",", "")
    if "." in num:
        value = float(num) * 2
        new = f"{value:g}"
    else:
        n = int(num)
        new = str(n + 1 if n < 10 or 1900 < n < 2100 else n * 2)
    if "," in raw:
        new = f"{int(new):,}" if new.isdigit() else new
    return answer[: m.start(1)] + new + answer[m.end(1) :]


def drop_qualifier(answer: str) -> str | None:
    """Remove the first qualifier phrase, so a bounded claim becomes an unbounded one."""
    for q in QUALIFIERS:
        pattern = re.compile(rf"\s+{re.escape(q)}(?=[\s,.;:)])", re.IGNORECASE)
        m = pattern.search(answer)
        if m:
            return answer[: m.start()] + answer[m.end() :]
    return None


def swap_citation(
    citations: Sequence[dict[str, Any]], pool: Sequence[tuple[str, str]]
) -> list[dict[str, Any]] | None:
    """Replace the first citation's quote with an unrelated quote from another answer."""
    if not citations:
        return None
    first = citations[0]
    for url, quote in pool:
        if quote and quote != first.get("quote"):
            return [{**first, "url": url, "quote": quote}, *citations[1:]]
    return None


def pad_answer(answer: str) -> str:
    return answer.rstrip() + PADDING


@dataclass(frozen=True)
class Variant:
    probe: str  # one of MUTANTS or PROBES
    kind: str  # "mutant" or "probe"
    output: dict[str, Any]
    salt: str


def variants(
    output: dict[str, Any],
    pool: Sequence[tuple[str, str]],
    *,
    salt: str = "judge",
    probe_salt: str = "probe",
) -> list[Variant]:
    """Every mutant and probe that can be built from one passed answer."""
    answer = str(output.get("answer", ""))
    raw = output.get("citations")
    cits = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
    out: list[Variant] = []
    if (changed := mutate_number(answer)) is not None:
        out.append(Variant("number", "mutant", {**output, "answer": changed}, salt))
    if (dropped := drop_qualifier(answer)) is not None:
        out.append(Variant("qualifier", "mutant", {**output, "answer": dropped}, salt))
    if (swapped := swap_citation(cits, pool)) is not None:
        out.append(Variant("citation", "mutant", {**output, "citations": swapped}, salt))
    out.append(Variant("padding", "probe", {**output, "answer": pad_answer(answer)}, salt))
    out.append(Variant("resample", "probe", dict(output), probe_salt))
    return out


def behaved(variant: Variant, original: dict[str, float], judged: dict[str, float]) -> bool:
    """Did the judge do what the variant demands?

    A mutant must fail on at least one verdict. A probe must reproduce every original verdict.
    An empty `judged` (the judge errored) counts as misbehaviour either way.
    """
    if not judged:
        return False
    if variant.kind == "mutant":
        return any(v < 1.0 for v in judged.values())
    return all(judged.get(metric) == value for metric, value in original.items())
