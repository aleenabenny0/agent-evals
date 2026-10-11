"""Summaries of a run, and paired comparisons between two runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from agent_evals.stats.core import (
    Estimate,
    bootstrap_ci,
    mcnemar_exact,
    mean_pass_hat_k,
    paired_bootstrap_diff,
    wilson_ci,
)
from agent_evals.store.db import ResultsStore
from agent_evals.suites import SuiteError, load_suite, paraphrase_groups

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
    # Paraphrase consistency, when the run's suite has paraphrase groups: the share of groups
    # whose members (source plus paraphrases) all got the same `correct` outcome.
    consistency: Estimate | None = None
    # `correct` per service area, when the run's suite records areas.
    by_area: dict[str, Estimate] = field(default_factory=dict)
    # Nominal cost divided by successful attempts; None without pricing or without a success.
    cost_per_success: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None


def paraphrase_consistency(
    correct_by_case: dict[str, list[float]], groups: dict[str, list[str]]
) -> Estimate | None:
    """One value per group: 1.0 if every member present in the run agrees on correct, else 0.0.

    A model that answers a question right but its paraphrase wrong is not robust; this measures
    how often that happens, independent of whether the answers were right.
    """
    values: list[float] = []
    for source, members in groups.items():
        outcomes = [
            all(v >= 1.0 for v in correct_by_case[cid])
            for cid in [source, *members]
            if cid in correct_by_case
        ]
        if len(outcomes) >= 2:
            values.append(1.0 if len(set(outcomes)) == 1 else 0.0)
    return _interval(values) if values else None


def _interval(case_means: list[float]) -> Estimate:
    """Wilson for a true proportion (every case exactly 0 or 1), bootstrap for anything else.

    The bootstrap collapses to zero width when every case agrees (7/7 -> [1, 1]); Wilson keeps
    honest width there. Counts, fractions and per-case means over several attempts are not
    proportions, so they keep the bootstrap. Paired comparisons always use the bootstrap.
    """
    if case_means and all(v in (0.0, 1.0) for v in case_means):
        return wilson_ci(int(sum(case_means)), len(case_means))
    return bootstrap_ci(case_means)


def summarize(store: ResultsStore, run_id: str) -> RunSummary:
    info = store.run_info(run_id)
    metrics: dict[str, Estimate] = {}
    for metric in store.metrics(run_id):
        per_case = store.metric_by_case(run_id, metric)
        case_means = [sum(v) / len(v) for v in per_case.values()]
        metrics[metric] = _interval(case_means)

    correct_by_case = store.metric_by_case(run_id, "correct")
    passes = [[v >= 1.0 for v in vals] for vals in correct_by_case.values()]
    k_max = min((len(p) for p in passes), default=0)
    phk = {k: mean_pass_hat_k(passes, k) for k in range(1, k_max + 1)} if passes else {}
    consistency = None
    by_area: dict[str, Estimate] = {}
    suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
    if suite_path.name and suite_path.exists():
        try:
            cases = load_suite(suite_path)
        except SuiteError:
            cases = []
        consistency = paraphrase_consistency(correct_by_case, paraphrase_groups(cases))
        area_of = {c.id: c.area for c in cases if c.area}
        per_area: dict[str, list[float]] = {}
        for case_id, vals in correct_by_case.items():
            area = area_of.get(case_id)
            if area:
                per_area.setdefault(area, []).append(sum(vals) / len(vals))
        by_area = {a: _interval(v) for a, v in sorted(per_area.items())}

    totals = store.totals(run_id)
    successes = sum(sum(1 for v in vals if v >= 1.0) for vals in correct_by_case.values())
    cost_per_success = (
        float(totals["cost_usd"]) / successes
        if totals["cost_usd"] is not None and successes
        else None
    )
    latencies = store.latencies(run_id)
    p50 = float(np.percentile(latencies, 50)) if latencies else None
    p95 = float(np.percentile(latencies, 95)) if latencies else None
    return RunSummary(
        run_id,
        info,
        metrics,
        phk,
        totals,
        store.failures(run_id),
        consistency,
        by_area,
        cost_per_success,
        p50,
        p95,
    )


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
    same_snapshot: bool | None  # None when a manifest doesn't record a fingerprint

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
    fp_a = store.run_info(baseline_id)["manifest"].get("corpus_fingerprint")
    fp_b = store.run_info(candidate_id)["manifest"].get("corpus_fingerprint")
    same_snapshot = (fp_a == fp_b) if fp_a is not None and fp_b is not None else None
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
        same_snapshot=same_snapshot,
    )
