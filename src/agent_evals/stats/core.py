"""Statistics for evals. Every reported number comes with its uncertainty.

Resampling is over cases, never attempts: attempts at one case are not independent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import comb, sqrt
from statistics import NormalDist

import numpy as np


@dataclass(frozen=True)
class Estimate:
    mean: float
    low: float
    high: float
    n: int
    method: str = "bootstrap"  # how the interval was computed: "bootstrap" or "wilson"


def bootstrap_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> Estimate:
    """Percentile bootstrap CI for the mean of per-case values."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        raise ValueError("no values")
    if arr.size == 1:
        v = float(arr[0])
        return Estimate(v, v, v, 1)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_resamples, arr.size))
    means = arr[idx].mean(axis=1)
    alpha = (1 - confidence) / 2
    low, high = np.quantile(means, [alpha, 1 - alpha])
    return Estimate(float(arr.mean()), float(low), float(high), int(arr.size))


def wilson_ci(successes: int, n: int, *, confidence: float = 0.95) -> Estimate:
    """Wilson score interval for a proportion: the true rates under which `successes` of `n`
    would not be surprising at this confidence.

    Unlike the bootstrap it keeps its width when every case agrees (7/7 -> about 0.65 to 1.00)
    and it never leaves [0, 1]. `mean` is the observed proportion, not the interval's centre.
    """
    if n <= 0:
        raise ValueError("need n > 0")
    if not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n")
    z = NormalDist().inv_cdf(0.5 + confidence / 2)  # 1.96 for 95%
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z / denom * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    # The ends are exactly 0 and 1 when p is; spell that out so rounding can't drift them.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return Estimate(p, low, high, n, method="wilson")


def paired_bootstrap_diff(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    confidence: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> Estimate:
    """CI for mean(candidate - baseline) over the same cases. Case difficulty cancels out."""
    if len(baseline) != len(candidate):
        raise ValueError("paired comparison needs the same cases in both runs")
    diffs = np.asarray(candidate, dtype=float) - np.asarray(baseline, dtype=float)
    return bootstrap_ci(diffs.tolist(), confidence=confidence, n_resamples=n_resamples, seed=seed)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Unbiased estimate of P(all k attempts pass) from c passes in n attempts (tau-bench)."""
    if not 0 <= c <= n:
        raise ValueError("need 0 <= c <= n")
    if not 1 <= k <= n:
        raise ValueError("need 1 <= k <= n")
    return comb(c, k) / comb(n, k)


def mean_pass_hat_k(per_case_passes: Sequence[Sequence[bool]], k: int) -> float:
    vals = [pass_hat_k(len(p), sum(p), k) for p in per_case_passes]
    return sum(vals) / len(vals)


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimate of P(at least one of k attempts passes) from c passes in n attempts.

    The complement of "all k sampled attempts fail": 1 - C(n-c, k) / C(n, k) (Chen et al., 2021).
    pass@1 is the plain pass rate. As k grows pass@k rises and pass^k falls; the gap between them
    is the ability the agent has but does not deliver to a user who asks once.
    """
    if not 0 <= c <= n:
        raise ValueError("need 0 <= c <= n")
    if not 1 <= k <= n:
        raise ValueError("need 1 <= k <= n")
    return 1.0 - comb(n - c, k) / comb(n, k)


def mean_pass_at_k(per_case_passes: Sequence[Sequence[bool]], k: int) -> float:
    vals = [pass_at_k(len(p), sum(p), k) for p in per_case_passes]
    return sum(vals) / len(vals)


def flip_rate(per_case_passes: Sequence[Sequence[bool]]) -> Estimate | None:
    """Share of cases whose repeated attempts disagree on pass/fail, with a Wilson interval.

    This is the noise floor: a difference between two runs no bigger than what identical attempts
    produce on their own is not evidence of anything. Only cases with at least two attempts
    count; None when there are none. Attempts carry distinct cache salts, so they are fresh
    samples, not replays of one answer.
    """
    flips = [len(set(p)) > 1 for p in per_case_passes if len(p) >= 2]
    if not flips:
        return None
    return wilson_ci(sum(flips), len(flips))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value. b: cases only the baseline passed; c: only candidate."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / (1 << n)  # P(X <= min) under H0
    return min(1.0, 2 * tail)
