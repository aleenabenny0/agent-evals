"""The online monitor's tests (phase 7 part 3, docs/plans/phase-7-online-evals.md).

Each metric compares one day with the frozen reference days, one-sided in its bad direction: a
rate by Fisher's exact test, a mean by a permutation test. The alert family is Holm-corrected,
and an alert also needs the effect to reach the metric's practical floor, so a tiny but
significant shift on a big day does not page anyone. Every metric reports the smallest true
rise that would alert on 8 days in 10, floor included, so a quiet day is read with its blind
spots in view: exactly for rates, by the normal approximation for means (decision 5, approved
by Aleena 2026-10-20).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import comb, exp, lgamma, log, log1p, sqrt
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


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed in log space so large n cannot overflow."""
    if k <= 0 or p >= 1.0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    lp, lq, ln = log(p), log1p(-p), lgamma(n + 1)
    tail = sum(
        exp(ln - lgamma(j + 1) - lgamma(n - j + 1) + j * lp + (n - j) * lq) for j in range(k, n + 1)
    )
    return min(1.0, tail)


def alert_count(
    bad_ref: int, n_ref: int, n_day: int, *, alpha: float, floor: float = 0.0, strict: bool = False
) -> int | None:
    """The fewest bad outcomes among the day's n_day that raise the alert against the frozen
    reference: Fisher's p at most alpha (below it, if strict) and the rise at least the floor.
    Fisher's p never rises as the count grows (checked exhaustively on small tables by the
    review), so the significant counts are a run up to n_day: the smallest is found by bisection,
    in about log2(n_day) tests. Every larger count alerts too. None when no count can."""
    if n_day == 0 or n_ref == 0:
        return None
    rate_ref = bad_ref / n_ref

    def significant(k: int) -> bool:
        p = fisher_greater(k, n_day, bad_ref, n_ref)
        return p < alpha if strict else p <= alpha

    if not significant(n_day):
        return None
    lo, hi = 0, n_day  # significant(hi) holds; find the smallest significant count
    while lo < hi:
        mid = (lo + hi) // 2
        if significant(mid):
            hi = mid
        else:
            lo = mid + 1
    reach = next(  # the fewest the floor allows; below int(...) - 1 it is out of reach
        (
            k
            for k in range(max(0, int(n_day * (rate_ref + floor)) - 1), n_day + 1)
            if k / n_day - rate_ref >= floor - 1e-12
        ),
        None,
    )
    return None if reach is None else max(lo, reach)


def rate_alert_power(
    rate_true: float,
    bad_ref: int,
    n_ref: int,
    n_day: int,
    *,
    alpha: float,
    floor: float = 0.0,
    strict: bool = False,
) -> float:
    """The chance that a day of n_day whose true bad rate is rate_true raises the alert, the
    reference frozen as it is: the binomial chance of reaching the alert count. Exact."""
    k = alert_count(bad_ref, n_ref, n_day, alpha=alpha, floor=floor, strict=strict)
    return 0.0 if k is None else binomial_tail(k, n_day, rate_true)


def detectable_rate_rise(
    *,
    bad_ref: int,
    n_ref: int,
    n_day: int,
    alpha: float,
    floor: float = 0.0,
    power: float = 0.8,
    strict: bool = False,
) -> float | None:
    """The smallest rise in the day's true bad rate that raises the alert with `power`, the
    reference frozen as it is. Exact: the true rate at which the day reaches the alert count
    (Fisher's test at alpha and the floor) with that chance. None when a side has no
    observations or no count can alert. (It replaced a normal approximation at the reference
    rate, which read about 0 at a reference rate of 0 and left out the floor.)"""
    k = alert_count(bad_ref, n_ref, n_day, alpha=alpha, floor=floor, strict=strict)
    if k is None:
        return None
    rate_ref = bad_ref / n_ref
    if binomial_tail(k, n_day, rate_ref) >= power:
        return 0.0
    lo, hi = rate_ref, 1.0
    for _ in range(60):  # the tail rises with the true rate: bisect for the power
        mid = (lo + hi) / 2
        if binomial_tail(k, n_day, mid) >= power:
            hi = mid
        else:
            lo = mid
    return hi - rate_ref


def detectable_mean_rise(
    sd_ref: float,
    n_day: int,
    n_ref: int,
    *,
    alpha: float,
    floor: float = 0.0,
    power: float = 0.8,
) -> float | None:
    """The smallest rise in a mean that raises the alert with `power`, the reference frozen as it
    is, by the normal approximation: the day's observed rise must clear both the test's critical
    value and the floor, and it varies around the true rise with the day's own standard error.
    Checked by simulating the real rule (the permutation test plus the floor) on days resampled
    from the real reference days and shifted by it (plan, "Blind spots")."""
    if n_day == 0 or n_ref == 0:
        return None
    critical = max(_NORMAL.inv_cdf(1 - alpha) * sd_ref * sqrt(1 / n_day + 1 / n_ref), floor)
    return critical + _NORMAL.inv_cdf(power) * sd_ref / sqrt(n_day)
