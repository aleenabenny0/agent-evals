"""Online runs (phase 7 part 2, docs/plans/phase-7-online-evals.md): a simulated day as cases.

A day's questions become `Case` objects in suite and split "online", provenance "simulated".
They have no reference answer, so they carry no gold, except a false premise: its contradicted
sentence becomes `gold.support`, the question's minimal key. The cases are written to a file
like any suite, so `evals judge`, `evals regrade` and `evals label` work on online runs as they
do on suite runs. The graders score only what needs no reference answer (the keyword grader
records `abstained` alone, the evidence metrics and the judge's responsive check are skipped),
and a judge grades an online run only under a pinned grader name, at its pinned version.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from agent_evals.simulate import TrafficQuestion
from agent_evals.types import Case, Gold

ONLINE_CASES_DIR = Path("results/online/cases")
ONLINE_SUITE_PREFIX = "online-"  # names a day's cases file; online-ness comes from the cases
# The judges that may grade online runs: one pin per grader name, each to one version, so a
# change of judge cannot pass for a change in the agent (docs/adr/0009). "judge" is the judge
# validated against the hand labels (docs/judge-validation.md): claude-sonnet-5-5 with rubric
# v2. A pin lets a judge grade online runs and nothing more: only a passed judge check of its
# own puts its judge_supported in the alert family (monitor.judge_standing).
ONLINE_JUDGE_PINS: Mapping[str, str] = MappingProxyType({"judge": "2cf75fa03e09"})
ONLINE_JUDGE_VERSION = ONLINE_JUDGE_PINS["judge"]  # the validated judge's pin


def judge_pin(grader: str) -> str | None:
    """The version a judge, by grader name, is pinned to for online runs; None when it has no
    pin, so it may not grade them."""
    return ONLINE_JUDGE_PINS.get(grader)


def pin_refusal(grader: str, version: str) -> str | None:
    """Why this judge may not grade an online run, or None when it may: its grader name must be
    pinned, and its version must be that pin."""
    pin = judge_pin(grader)
    if pin is None:
        pinned = ", ".join(
            f"the validated judge {v}" if name == "judge" else f"{name} at {v}"
            for name, v in sorted(ONLINE_JUDGE_PINS.items(), key=lambda kv: kv[0] != "judge")
        )
        return (
            f"An online run is judged only by a pinned judge: {pinned}. This config's judge, "
            f"{grader}, has no pin"
        )
    if version == pin:
        return None
    if grader == "judge":
        return (
            f"An online run is judged only by the validated judge {pin}; this config's judge is "
            f"{version}"
        )
    return (
        f"An online run is judged by {grader} only at its pinned version {pin}; this config's "
        f"{grader} is {version}"
    )


def online_suite_name(day: int) -> str:
    return f"{ONLINE_SUITE_PREFIX}day-{day:02d}"


def online_case(q: TrafficQuestion) -> Case:
    return Case.model_validate(
        {
            "id": q.id,
            "suite": "online",
            "agent": "docs",
            "type": q.type,
            "question": q.question,
            "gold": Gold(answer="", support=[q.contradicts]) if q.contradicts else None,
            "split": "online",
            "provenance": "simulated",
            "area": q.area,
            "notes": f"persona: {q.persona}; intent: {q.intent}",
        }
    )
