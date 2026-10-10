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
    "malformed_output",
    "rejected_submissions",
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
    lower_is_better: bool
    n_cases: int
    baseline: float
    candidate: float
    diff: Estimate
    only_baseline_passed: list[str]
    only_candidate_passed: list[str]
    p_value: float

    @property
    def verdict(self) -> str:
        """'improved' or 'regressed' only when the 95% CI excludes zero in that direction."""
        low, high = self.diff.low, self.diff.high
        if self.lower_is_better:
            low, high = -high, -low  # flip so that a positive difference always means better
        if low > 0:
            return "improved"
        if high < 0:
            return "regressed"
        return "no detectable change"


def _passes(mean: float, lower_is_better: bool) -> bool:
    """A case passes when it passes on a strict majority of attempts."""
    return mean < 0.5 if lower_is_better else mean > 0.5


def compare(
    store: ResultsStore, baseline_id: str, candidate_id: str, metric: str = "correct"
) -> Comparison:
    a = store.metric_by_case(baseline_id, metric)
    b = store.metric_by_case(candidate_id, metric)
    shared = sorted(set(a) & set(b))
    if not shared:
        raise ValueError(f"the two runs share no cases graded on {metric!r}")
    lower = metric in LOWER_IS_BETTER
    a_means = [sum(a[c]) / len(a[c]) for c in shared]
    b_means = [sum(b[c]) / len(b[c]) for c in shared]
    a_pass = [_passes(x, lower) for x in a_means]
    b_pass = [_passes(y, lower) for y in b_means]
    only_a = [c for c, x, y in zip(shared, a_pass, b_pass, strict=True) if x and not y]
    only_b = [c for c, x, y in zip(shared, a_pass, b_pass, strict=True) if y and not x]
    return Comparison(
        metric=metric,
        lower_is_better=lower,
        n_cases=len(shared),
        baseline=sum(a_means) / len(shared),
        candidate=sum(b_means) / len(shared),
        diff=paired_bootstrap_diff(a_means, b_means),
        only_baseline_passed=only_a,
        only_candidate_passed=only_b,
        p_value=mcnemar_exact(len(only_a), len(only_b)),
    )
