"""The online monitor's tests (phase 7 part 3, docs/plans/phase-7-online-evals.md).

Each metric compares one day with the frozen reference days, one-sided in its bad direction: a
rate by Fisher's exact test, a mean by a permutation test. The alert family is Holm-corrected,
and an alert also needs the effect to reach the metric's practical floor, so a tiny but
significant shift on a big day does not page anyone. Every test reports the smallest effect it
could detect at the day's n, so a quiet day is read with its blind spots in view.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import comb, sqrt
from statistics import NormalDist

import numpy as np

_NORMAL = NormalDist()
MIN_DRAWS = 20_000  # the plan's floor for permutation tests


def fisher_greater(bad_day: int, n_day: int, bad_ref: int, n_ref: int) -> float:
    """One-sided Fisher exact p-value that the day's bad rate is higher than the reference's:
    with the margins fixed, the chance of at least `bad_day` bad outcomes among the day's."""
    if not 0 <= bad_day <= n_day or not 0 <= bad_ref <= n_ref:
        raise ValueError("counts must lie between 0 and their n")
    if n_day == 0 or n_ref == 0:
        return 1.0
    bad, total = bad_day + bad_ref, n_day + n_ref
    tail = sum(
        comb(bad, k) * comb(total - bad, n_day - k) for k in range(bad_day, min(bad, n_day) + 1)
    )
    return min(1.0, tail / comb(total, n_day))


def permutation_greater(
    day: Sequence[float], ref: Sequence[float], *, draws: int = MIN_DRAWS, seed: int = 0
) -> float:
    """One-sided permutation p-value that the day's mean is higher than the reference's, over
    `draws` random relabelings of the pooled values: p = (k + 1) / (R + 1), so never 0."""
    if draws < MIN_DRAWS:
        raise ValueError(f"use at least {MIN_DRAWS} draws")
    if not day or not ref:
        return 1.0
    pooled = np.asarray([*day, *ref], dtype=float)
    n = len(day)
    observed = pooled[:n].mean() - pooled[n:].mean()
    rng = np.random.default_rng(seed)
    hits = 0
    for start in range(0, draws, 2_000):  # in batches, to bound memory
        size = min(2_000, draws - start)
        order = rng.random((size, pooled.size)).argsort(axis=1)
        shuffled = pooled[order]
        diff = shuffled[:, :n].mean(axis=1) - shuffled[:, n:].mean(axis=1)
        hits += int((diff >= observed - 1e-12).sum())
    return (hits + 1) / (draws + 1)


def holm(pvalues: Mapping[str, float], *, alpha: float = 0.05) -> dict[str, bool]:
    """Holm's step-down correction: which hypotheses are rejected at family-wise `alpha`."""
    order = sorted(pvalues, key=lambda name: pvalues[name])
    rejected: dict[str, bool] = {}
    open_ = True
    for i, name in enumerate(order):
        open_ = open_ and pvalues[name] <= alpha / (len(order) - i)
        rejected[name] = open_
    return rejected


def detectable_rate_rise(
    rate_ref: float, n_day: int, n_ref: int, *, alpha: float, power: float = 0.8
) -> float | None:
    """The smallest rise in a rate that a one-sided test at `alpha` detects with `power`, by the
    normal approximation at the reference rate. None when a side has no observations."""
    if n_day == 0 or n_ref == 0:
        return None
    z = _NORMAL.inv_cdf(1 - alpha) + _NORMAL.inv_cdf(power)
    p = min(max(rate_ref, 1e-6), 1 - 1e-6)
    return z * sqrt(p * (1 - p) * (1 / n_day + 1 / n_ref))


def detectable_mean_rise(
    sd_ref: float, n_day: int, n_ref: int, *, alpha: float, power: float = 0.8
) -> float | None:
    """The smallest rise in a mean that a one-sided test at `alpha` detects with `power`."""
    if n_day == 0 or n_ref == 0:
        return None
    z = _NORMAL.inv_cdf(1 - alpha) + _NORMAL.inv_cdf(power)
    return z * sd_ref * sqrt(1 / n_day + 1 / n_ref)
