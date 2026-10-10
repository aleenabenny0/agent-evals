"""How many paired cases a comparison needs to detect a change in a pass rate.

The release gate compares a candidate against the champion on the same cases (`report.compare`),
so only cases where the two disagree carry information. Their share is the *discordance* d. For
a true difference `delta` in pass rate, McNemar's statistic is

    (c - b) / sqrt(b + c) = delta * sqrt(n / d)

where c and b are the cases only the candidate / only the baseline passes. Detecting the change
with the requested power at a two-sided level alpha needs that statistic to reach
z(1 - alpha/2) + z(power), which gives the sample sizes below. `tests/test_power.py` simulates
paired outcomes and checks that this normal approximation describes what the paired bootstrap
in `report.compare` actually does.
"""

from __future__ import annotations

from math import ceil, sqrt
from statistics import NormalDist

_NORMAL = NormalDist()


def _check_rates(delta: float, discordance: float) -> None:
    if not 0 < delta <= 1:
        raise ValueError("need 0 < delta <= 1")
    if not delta <= discordance <= 1:
        raise ValueError(
            "need delta <= discordance <= 1: a net change of delta needs at least that many "
            "disagreeing cases"
        )


def _check_levels(alpha: float, power: float) -> None:
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("need 0 < alpha < 1 and 0 < power < 1")


def _z_total(alpha: float, power: float) -> float:
    return _NORMAL.inv_cdf(1 - alpha / 2) + _NORMAL.inv_cdf(power)


def cases_for_paired_delta(
    delta: float, discordance: float, *, alpha: float = 0.05, power: float = 0.8
) -> int:
    """Smallest number of paired cases that detects a pass-rate change of `delta`."""
    _check_rates(delta, discordance)
    _check_levels(alpha, power)
    return ceil(_z_total(alpha, power) ** 2 * discordance / delta**2)


def detectable_delta(
    n: int, discordance: float, *, alpha: float = 0.05, power: float = 0.8
) -> float:
    """Smallest pass-rate change that `n` paired cases detect.

    A result above `discordance` means no change at that discordance is detectable with `n`.
    """
    if n <= 0:
        raise ValueError("need n > 0")
    if not 0 < discordance <= 1:
        raise ValueError("need 0 < discordance <= 1")
    _check_levels(alpha, power)
    return _z_total(alpha, power) * sqrt(discordance / n)


def paired_power(n: int, delta: float, discordance: float, *, alpha: float = 0.05) -> float:
    """Probability that `n` paired cases detect a true change of `delta` at this discordance."""
    if n <= 0:
        raise ValueError("need n > 0")
    _check_rates(delta, discordance)
    _check_levels(alpha, 0.5)
    return _NORMAL.cdf(delta * sqrt(n / discordance) - _NORMAL.inv_cdf(1 - alpha / 2))
