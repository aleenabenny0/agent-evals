"""Machine checks on a code case, run before a person reviews it (phase 3, part 4).

A possible task passes when its setup runs, its reference solution passes the hidden test, and
both a solution that does nothing and the case's near miss fail it, at the test and not before.
So the test is known to see the task: it is not satisfied by an empty account or by a solution
that misses the point. The reference must also pass the static API check, so the reference itself
calls nothing that does not exist.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from agent_evals.agents.code.sandbox import CodeRunner, Job, RunResult
from agent_evals.graders.api_check import check_api
from agent_evals.types import Case

NOTHING = "def solve(session):\n    return None\n"


@dataclass
class CaseCheck:
    case_id: str
    errors: list[str] = field(default_factory=list)
    runs: dict[str, RunResult] = field(default_factory=dict)  # reference, nothing, near_miss

    @property
    def ok(self) -> bool:
        return not self.errors


def check_case(case: Case, runner: CodeRunner) -> CaseCheck:
    held = CaseCheck(case.id)
    task = case.task
    if task is None:
        held.errors.append("not a code case")
        return held
    if case.type == "unanswerable":
        return held  # nothing to run: the right outcome is an abstention
    assert task.reference is not None and task.near_miss is not None and task.test is not None
    api = check_api(task.reference)
    for problem in api.hallucinated_operations + api.hallucinated_parameters:
        held.errors.append(f"the reference calls what does not exist: {problem}")
    for problem in api.missing_required:
        held.errors.append(f"the reference misses a required parameter: {problem}")
    for name, code in (
        ("reference", task.reference),
        ("nothing", NOTHING),
        ("near_miss", task.near_miss),
    ):
        held.runs[name] = runner.run(Job(code=code, setup=task.setup, test=task.test))
    reference, nothing, near = held.runs["reference"], held.runs["nothing"], held.runs["near_miss"]
    if reference.case_defect:
        held.errors.append(f"the setup fails: {reference.error}")
        return held
    if not reference.ok:
        held.errors.append(f"the reference fails at {reference.stage}: {reference.error}")
    for name, run in (("a solution that does nothing", nothing), ("the near miss", near)):
        if run.ok:
            held.errors.append(f"{name} passes the test, so the test does not see the task")
        elif run.stage != "test":
            held.errors.append(f"{name} fails at {run.stage}, before the test: {run.error}")
    return held


def check_cases(cases: list[Case], runner: CodeRunner, workers: int = 4) -> list[CaseCheck]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: check_case(c, runner), cases))
