"""Summaries of a run, and paired comparisons between two runs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from agent_evals.stats.core import (
    Estimate,
    bootstrap_ci,
    flip_rate,
    mcnemar_exact,
    mean_pass_at_k,
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
    "repeated_calls",
    "unsupported_entity",
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
    # P(at least one of k attempts passes), beside pass_hat_k's P(all k pass).
    pass_at_k: dict[int, float] = field(default_factory=dict)
    # Share of cases whose repeated attempts disagree; None with a single attempt per case.
    flip_rate: Estimate | None = None


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


def other_judges(store: ResultsStore, run_id: str, judge: str) -> tuple[str, ...]:
    """Judge graders on the run other than `judge`, whose grades a summary must leave out."""
    return tuple(g for g in store.judge_graders(run_id) if g != judge)


def summarize(store: ResultsStore, run_id: str, judge: str = "judge") -> RunSummary:
    """Every metric of a run with its interval. Judge metrics come from the grader named
    `judge` only; a candidate judge's grades on the same answers are left out."""
    info = store.run_info(run_id)
    skip = other_judges(store, run_id, judge)
    metrics: dict[str, Estimate] = {}
    for metric in store.metrics(run_id, skip):
        per_case = store.metric_by_case(run_id, metric, skip)
        case_means = [sum(v) / len(v) for v in per_case.values()]
        metrics[metric] = _interval(case_means)

    correct_by_case = store.metric_by_case(run_id, "correct")
    passes = [[v >= 1.0 for v in vals] for vals in correct_by_case.values()]
    k_max = min((len(p) for p in passes), default=0)
    phk = {k: mean_pass_hat_k(passes, k) for k in range(1, k_max + 1)} if passes else {}
    pak = {k: mean_pass_at_k(passes, k) for k in range(1, k_max + 1)} if passes else {}
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
        pak,
        flip_rate(passes),
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


# Metrics the gate watches for regressions besides the target (ADR 0008, condition 2).
MONITORED = (
    "correct",
    "fabricated_citation",
    "uncited_answer",
    "wrong_abstention",
    "missed_abstention",
    "rejected_submissions",
)


@dataclass
class CategoryDiff:
    kind: str  # "type" or "area"
    name: str
    n: int
    diff: Estimate  # paired difference in `correct`, challenger minus champion

    @property
    def regressed(self) -> bool:
        return self.diff.high < 0


@dataclass
class GateResult:
    target: Comparison
    monitored: list[Comparison]
    skipped: list[str]  # monitored metrics absent from one of the runs
    categories: list[CategoryDiff]

    @property
    def regressed_metrics(self) -> list[str]:
        return [c.metric for c in self.monitored if c.verdict == "regressed"]

    @property
    def regressed_categories(self) -> list[str]:
        return [f"{c.kind}={c.name}" for c in self.categories if c.regressed]

    @property
    def passed(self) -> bool:
        """ADR 0008: target improved with its interval, no monitored metric regressed with its
        interval, no type or area regressed on `correct` with its paired interval."""
        return (
            self.target.verdict == "improved"
            and not self.regressed_metrics
            and not self.regressed_categories
        )


def gate(
    store: ResultsStore,
    champion: str,
    challenger: str,
    cases: Sequence[Any],
    target: str = "judge_supported",
    judge: str = "judge",
) -> GateResult:
    """The champion-versus-challenger gate on one suite (dev), read from the store. Judge
    metrics come from the grader named `judge` on both runs."""
    target_cmp = compare(store, champion, challenger, target, judge)
    monitored: list[Comparison] = []
    skipped: list[str] = []
    for metric in MONITORED:
        if metric == target:
            continue
        try:
            monitored.append(compare(store, champion, challenger, metric, judge))
        except ValueError:
            skipped.append(metric)
    a = store.metric_by_case(champion, "correct")
    b = store.metric_by_case(challenger, "correct")
    categories: list[CategoryDiff] = []
    keys: list[tuple[str, Callable[[Any], str]]] = [
        ("type", lambda c: str(c.type)),
        ("area", lambda c: str(c.area or "")),
    ]
    for kind, key in keys:
        groups: dict[str, list[str]] = {}
        for c in cases:
            if c.id in a and c.id in b:
                groups.setdefault(key(c), []).append(c.id)
        for name, ids in sorted(groups.items()):
            x = [sum(a[i]) / len(a[i]) for i in ids]
            y = [sum(b[i]) / len(b[i]) for i in ids]
            categories.append(CategoryDiff(kind, name, len(ids), paired_bootstrap_diff(x, y)))
    return GateResult(target_cmp, monitored, skipped, categories)


def _passes(mean: float, lower_is_better: bool) -> bool:
    """A case passes when it passes on a strict majority of attempts."""
    return mean < 0.5 if lower_is_better else mean > 0.5


def compare(
    store: ResultsStore,
    baseline_id: str,
    candidate_id: str,
    metric: str = "correct",
    judge: str = "judge",
) -> Comparison:
    """Paired comparison on the shared cases; judge metrics from the grader named `judge`."""
    a = store.metric_by_case(baseline_id, metric, other_judges(store, baseline_id, judge))
    b = store.metric_by_case(candidate_id, metric, other_judges(store, candidate_id, judge))
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
