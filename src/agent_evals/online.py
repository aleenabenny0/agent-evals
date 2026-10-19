"""Online runs (phase 7 part 2, docs/plans/phase-7-online-evals.md): a simulated day as cases.

A day's questions become `Case` objects in suite and split "online", provenance "simulated".
They have no reference answer, so they carry no gold, except a false premise: its contradicted
sentence becomes `gold.support`, the question's minimal key. The cases are written to a file
like any suite, so `evals judge`, `evals regrade` and `evals label` work on online runs as they
do on suite runs. The graders score only what needs no reference answer (the keyword grader
records `abstained` alone, the evidence metrics and the judge's responsive check are skipped),
and the judge must be the validated one, pinned by version.
"""

from __future__ import annotations

from pathlib import Path

from agent_evals.simulate import TrafficQuestion
from agent_evals.types import Case, Gold

ONLINE_CASES_DIR = Path("results/online/cases")
ONLINE_SUITE_PREFIX = "online-"  # an online run's suite name; never the latest run
# The judge validated against Aleena's labels (docs/judge-validation.md): claude-sonnet-5-5 with
# rubric v2. Only it judges online runs, so a change of judge cannot pass for a change in the
# agent.
ONLINE_JUDGE_VERSION = "2cf75fa03e09"


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
