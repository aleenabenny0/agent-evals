"""The monitor's tests behave as their definitions say, checked against exact or simulated truth."""

from __future__ import annotations

from math import comb

import numpy as np
import pytest

from agent_evals.stats.monitor import (
    detectable_mean_rise,
    detectable_rate_rise,
    fisher_greater,
    holm,
    permutation_greater,
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
    small = detectable_rate_rise(0.3, 100, 500, alpha=0.05 / 7)
    big = detectable_rate_rise(0.3, 200, 500, alpha=0.05 / 7)
    assert small is not None and big is not None and big < small
    assert detectable_rate_rise(0.3, 0, 500, alpha=0.05) is None
    mean = detectable_mean_rise(1.0, 100, 400, alpha=0.05)
    assert mean is not None and 0.2 < mean < 0.4  # 2.49 x sqrt(1/100 + 1/400) = 0.28
