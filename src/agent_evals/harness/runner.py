"""Runs a suite: every (case, attempt) through the agent, then every grader, then the store."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from rich.progress import Progress

from agent_evals.config import Pricing
from agent_evals.store.db import ResultsStore
from agent_evals.types import Agent, AgentResult, Case, Grade, Grader, RunContext, Step


def new_run_id(agent_name: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{agent_name}-{stamp}-{uuid.uuid4().hex[:6]}"


def _safe_run(agent: Agent, case: Case, ctx: RunContext) -> AgentResult:
    """An exception inside one case must never take down the whole run."""
    try:
        return agent.run(case, ctx)
    except Exception as exc:
        return AgentResult(error=f"{type(exc).__name__}: {exc}")


def grade_all(
    graders: Sequence[Grader], case: Case, result: AgentResult
) -> list[tuple[str, Grade]]:
    graded: list[tuple[str, Grade]] = []
    for grader in graders:
        try:
            graded.extend((grader.name, g) for g in grader.grade(case, result))
        except Exception as exc:
            graded.append(
                (
                    grader.name,
                    Grade(
                        metric="grader_error",
                        value=1.0,
                        passed=False,
                        reason=f"{type(exc).__name__}: {exc}",
                    ),
                )
            )
    return graded


def regrade_run(
    store: ResultsStore, run_id: str, cases: Sequence[Case], graders: Sequence[Grader]
) -> int:
    """Re-run code graders over a run's stored attempts against the cases as they are now.

    For when a case's `must_include` list or a grader changes after the run: the agent is not
    re-run (its answers are stored), only the deterministic grades are replaced, grader by
    grader. Grades from graders not in `graders` (the LLM judge, for example) are untouched.
    Returns the number of attempts regraded. The manifest keeps the original grader versions;
    the new ones are added under `regraded`.
    """
    by_id = {c.id: c for c in cases}
    attempts = [a for a in store.all_attempts(run_id) if a["case_id"] in by_id]
    for grader in graders:
        store.delete_grades(run_id, grader.name)
    for a in attempts:
        result = AgentResult(
            output=a["output"],
            trajectory=[Step.model_validate(s) for s in a["trajectory"]],
            error=a["error"],
        )
        store.add_grades(
            run_id, a["case_id"], a["attempt"], grade_all(graders, by_id[a["case_id"]], result)
        )
    store.annotate_run(
        run_id,
        "regraded",
        {
            "at": datetime.now(UTC).isoformat(),
            "graders": {g.name: g.version for g in graders},
            "cases": len(by_id),
        },
    )
    return len(attempts)


def run_suite(
    *,
    agent: Agent,
    cases: Sequence[Case],
    graders: Sequence[Grader],
    store: ResultsStore,
    suite_name: str,
    attempts: int,
    workers: int,
    pricing: Pricing,
    budget_usd: float | None,
    manifest: dict[str, Any],
    run_id: str | None = None,
    show_progress: bool = True,
) -> str:
    run_id = run_id or new_run_id(agent.name)
    store.start_run(
        run_id,
        agent=agent.name,
        agent_version=agent.version,
        suite=suite_name,
        attempts=attempts,
        manifest=manifest,
    )

    jobs = iter([(case, attempt) for case in cases for attempt in range(1, attempts + 1)])
    total = len(cases) * attempts
    workers = max(1, workers)
    spent = 0.0
    status = "complete"

    with (
        ThreadPoolExecutor(max_workers=workers) as pool,
        Progress(disable=not show_progress, transient=True) as progress,
    ):
        task = progress.add_task(f"Running {suite_name}", total=total)
        in_flight: dict[Future[AgentResult], tuple[Case, int]] = {}

        def submit_next() -> bool:
            job = next(jobs, None)
            if job is None:
                return False
            case, attempt = job
            ctx = RunContext(run_id=run_id, case_id=case.id, attempt=attempt)
            in_flight[pool.submit(_safe_run, agent, case, ctx)] = job
            return True

        # Only `workers` jobs are ever queued, so a budget stop takes effect immediately.
        for _ in range(workers):
            submit_next()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                case, attempt = in_flight.pop(future)
                result = future.result()
                cost = result.usage.cost_usd(pricing)
                # Only uncached calls cost money; scale nominal cost by the uncached share.
                if cost is not None and result.usage.model_calls:
                    uncached = result.usage.model_calls - result.usage.cached_calls
                    spent += cost * uncached / result.usage.model_calls
                store.record_attempt(
                    run_id, case.id, attempt, result, cost, grade_all(graders, case, result)
                )
                progress.advance(task)
                if budget_usd is not None and spent > budget_usd:
                    status = "budget_exceeded"
                if status == "complete":
                    submit_next()

    store.finish_run(run_id, status)
    return run_id
