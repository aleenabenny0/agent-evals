"""Summaries of a run, and paired comparisons between two runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_evals.stats.core import (
    Estimate,
    bootstrap_ci,
    mcnemar_exact,
    mean_pass_hat_k,
    paired_bootstrap_diff,
)
from agent_evals.store.db import ResultsStore

# Direction of "better" per metric, shown so the reader doesn't have to guess.
NEUTRAL = {"abstained"}  # depends on the case type; see wrong/missed_abstention
LOWER_IS_BETTER = {
    "fabricated_citation",
    "uncited_answer",
    "missed_abstention",
    "wrong_abstention",
    "tool_errors",
    "model_calls",
    "grader_error",
}


@dataclass
class RunSummary:
    run_id: str
    info: dict[str, Any]
    metrics: dict[str, Estimate]
    pass_hat_k: dict[int, float]
    totals: dict[str, Any]
    failures: list[tuple[str, int, str]]


def summarize(store: ResultsStore, run_id: str) -> RunSummary:
    info = store.run_info(run_id)
    metrics: dict[str, Estimate] = {}
    for metric in store.metrics(run_id):
        per_case = store.metric_by_case(run_id, metric)
        case_means = [sum(v) / len(v) for v in per_case.values()]
        metrics[metric] = bootstrap_ci(case_means)

    passes = [[v >= 1.0 for v in vals] for vals in store.metric_by_case(run_id, "correct").values()]
    k_max = min((len(p) for p in passes), default=0)
    phk = {k: mean_pass_hat_k(passes, k) for k in range(1, k_max + 1)} if passes else {}
    return RunSummary(run_id, info, metrics, phk, store.totals(run_id), store.failures(run_id))


@dataclass
class Comparison:
    metric: str
    n_cases: int
    baseline: float
    candidate: float
    diff: Estimate
    only_baseline_passed: list[str]
    only_candidate_passed: list[str]
    p_value: float


def compare(
    store: ResultsStore, baseline_id: str, candidate_id: str, metric: str = "correct"
) -> Comparison:
    a = store.metric_by_case(baseline_id, metric)
    b = store.metric_by_case(candidate_id, metric)
    shared = sorted(set(a) & set(b))
    if not shared:
        raise ValueError("the two runs share no cases")
    a_means = [sum(a[c]) / len(a[c]) for c in shared]
    b_means = [sum(b[c]) / len(b[c]) for c in shared]
    # A case "passes" when it passes on a strict majority of attempts.
    only_a = [c for c, x, y in zip(shared, a_means, b_means, strict=True) if x > 0.5 >= y]
    only_b = [c for c, x, y in zip(shared, a_means, b_means, strict=True) if y > 0.5 >= x]
    return Comparison(
        metric=metric,
        n_cases=len(shared),
        baseline=sum(a_means) / len(shared),
        candidate=sum(b_means) / len(shared),
        diff=paired_bootstrap_diff(a_means, b_means),
        only_baseline_passed=only_a,
        only_candidate_passed=only_b,
        p_value=mcnemar_exact(len(only_a), len(only_b)),
    )
