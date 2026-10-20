"""The monitor's tests behave as their definitions say, checked against exact or simulated truth."""

from __future__ import annotations

from math import comb

import numpy as np
import pytest

from agent_evals.stats.monitor import (
    alert_count,
    binomial_tail,
    detectable_mean_rise,
    detectable_rate_rise,
    fisher_greater,
    holm,
    permutation_greater,
    rate_alert_power,
)


def test_fisher_greater_matches_the_hypergeometric_tail_and_its_edges() -> None:
    # 2 x 2 table: day 5 bad of 10, reference 1 bad of 10. P(X >= 5), X ~ Hypergeom(20, 6, 10).
    expected = sum(comb(6, k) * comb(14, 10 - k) for k in range(5, 7)) / comb(20, 10)
    assert fisher_greater(5, 10, 1, 10) == pytest.approx(expected)
    assert fisher_greater(0, 10, 5, 10) == pytest.approx(1.0)  # no rise: no evidence
    assert fisher_greater(1, 10, 9, 10) > 0.99  # a fall is never an alert
    assert fisher_greater(0, 0, 3, 10) == 1.0
    with pytest.raises(ValueError):
        fisher_greater(11, 10, 0, 10)


def test_fisher_greater_holds_its_level_when_nothing_changed() -> None:
    rng = np.random.default_rng(1)
    hits = 0
    for _ in range(400):
        day, ref = rng.binomial(100, 0.2), rng.binomial(400, 0.2)
        hits += fisher_greater(int(day), 100, int(ref), 400) <= 0.05
    assert hits / 400 <= 0.05 + 0.02  # exact tests are conservative; allow simulation noise


def test_permutation_greater_detects_a_rise_and_never_returns_zero() -> None:
    rng = np.random.default_rng(2)
    ref = list(rng.normal(0.0, 1.0, 200))
    assert permutation_greater(list(rng.normal(1.0, 1.0, 50)), ref) < 0.01
    flat = permutation_greater(list(rng.normal(0.0, 1.0, 50)), ref, seed=3)
    assert flat > 0.01
    assert permutation_greater([10.0] * 50, [0.0] * 50) == pytest.approx(1 / 20_001, rel=0.5)
    assert permutation_greater([], ref) == 1.0
    with pytest.raises(ValueError):
        permutation_greater([1.0], [0.0], draws=100)  # under the plan's floor


def test_holm_steps_down_and_stops_at_the_first_failure() -> None:
    p = {"a": 0.001, "b": 0.012, "c": 0.03, "d": 0.2}
    # thresholds: 0.05/4 = 0.0125, 0.05/3 = 0.0167, 0.05/2 = 0.025, 0.05/1 = 0.05
    assert holm(p) == {"a": True, "b": True, "c": False, "d": False}
    assert holm({"a": 0.02, "b": 0.001}) == {"b": True, "a": True}
    assert holm({"a": 0.04, "b": 0.03}) == {"b": False, "a": False}  # 0.03 > 0.025


def test_detectable_effects_shrink_with_n_and_vanish_without_data() -> None:
    small = detectable_rate_rise(bad_ref=150, n_ref=500, n_day=100, alpha=0.05 / 7)
    big = detectable_rate_rise(bad_ref=150, n_ref=500, n_day=200, alpha=0.05 / 7)
    assert small is not None and big is not None and big < small
    assert detectable_rate_rise(bad_ref=150, n_ref=500, n_day=0, alpha=0.05) is None
    assert detectable_rate_rise(bad_ref=0, n_ref=10, n_day=3, alpha=1e-9) is None  # never alerts
    mean = detectable_mean_rise(1.0, 100, 400, alpha=0.05)
    assert mean is not None and 0.2 < mean < 0.4  # 1.64 x sqrt(1/100 + 1/400) + 0.84 x 0.1
    assert detectable_mean_rise(1.0, 100, 400, alpha=0.05, floor=0.5) == pytest.approx(
        0.5 + 0.8416 * 0.1, abs=1e-3
    )  # the floor binds: the observed rise must reach 0.5, then 80% power on top


def test_a_zero_reference_rate_still_needs_real_failures() -> None:
    # The real uncited case: 0 of 431 on the reference days, a day of 86. The old normal
    # approximation said a 0.04-point rise was detectable; an alert needs 5 uncited (the 5-point
    # floor), which a day reaches 80% of the time only at a true rate near 7.7%.
    assert alert_count(0, 431, 86, alpha=0.05 / 6, floor=0.05) == 5
    mde = detectable_rate_rise(bad_ref=0, n_ref=431, n_day=86, alpha=0.05 / 6, floor=0.05)
    assert mde == pytest.approx(0.077, abs=0.002)
    assert binomial_tail(5, 86, mde) == pytest.approx(0.8, abs=1e-6)


def test_the_detectable_rise_alerts_8_days_in_10_by_simulating_the_rule() -> None:
    rng = np.random.default_rng(7)
    for bad_ref, n_ref, n_day, floor in (
        (12, 398, 80, 0.05),
        (3, 500, 100, 0.02),
        (322, 431, 86, 0.05),
    ):
        alpha = 0.05 / 6
        mde = detectable_rate_rise(
            bad_ref=bad_ref, n_ref=n_ref, n_day=n_day, alpha=alpha, floor=floor
        )
        assert mde is not None
        rate = bad_ref / n_ref + mde
        days = rng.binomial(n_day, rate, 4_000)
        alerts = [  # the monitor's rule, applied directly to each simulated day
            fisher_greater(int(k), n_day, bad_ref, n_ref) <= alpha
            and k / n_day - bad_ref / n_ref >= floor - 1e-12
            for k in days
        ]
        assert np.mean(alerts) == pytest.approx(0.8, abs=0.025)
        assert rate_alert_power(rate, bad_ref, n_ref, n_day, alpha=alpha, floor=floor) == (
            pytest.approx(0.8, abs=1e-6)
        )


def test_the_fabrication_rule_is_strict_and_has_no_floor() -> None:
    # 12 of 431 on the reference days, a day of 86, p below 0.01: the real fabricated rule
    k = alert_count(12, 431, 86, alpha=0.01, strict=True)
    assert k is not None and fisher_greater(k, 86, 12, 431) < 0.01
    assert fisher_greater(k - 1, 86, 12, 431) >= 0.01
    mde = detectable_rate_rise(bad_ref=12, n_ref=431, n_day=86, alpha=0.01, strict=True)
    assert mde == pytest.approx(0.089, abs=0.003)
