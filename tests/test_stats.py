from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_evals.stats.core import (
    bootstrap_ci,
    mcnemar_exact,
    paired_bootstrap_diff,
    pass_hat_k,
    wilson_ci,
)

unit_values = st.lists(st.floats(0, 1), min_size=2, max_size=60)


@given(unit_values)
def test_ci_brackets_the_mean(values: list[float]) -> None:
    est = bootstrap_ci(values, n_resamples=500)
    assert est.low - 1e-9 <= est.mean <= est.high + 1e-9
    assert est.low >= 0.0 and est.high <= 1.0


def test_ci_is_deterministic_for_a_seed() -> None:
    vals = [0, 1, 1, 0, 1, 1, 1, 0]
    assert bootstrap_ci(vals, seed=3) == bootstrap_ci(vals, seed=3)


def test_constant_values_have_zero_width() -> None:
    est = bootstrap_ci([1.0] * 20)
    assert est.low == est.high == 1.0


def test_paired_diff_of_identical_runs_is_zero() -> None:
    est = paired_bootstrap_diff([0, 1, 1, 0], [0, 1, 1, 0])
    assert est.mean == est.low == est.high == 0.0


def test_paired_diff_needs_same_length() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap_diff([1, 0], [1])


@given(st.integers(1, 10).flatmap(lambda n: st.tuples(st.just(n), st.integers(0, n))))
def test_pass_hat_k_properties(nc: tuple[int, int]) -> None:
    n, c = nc
    assert pass_hat_k(n, c, 1) == pytest.approx(c / n)
    # Requiring more consecutive successes can only make it harder.
    values = [pass_hat_k(n, c, k) for k in range(1, n + 1)]
    assert all(a >= b - 1e-12 for a, b in itertools.pairwise(values))
    assert pass_hat_k(n, n, n) == 1.0


def test_pass_hat_k_known_value() -> None:
    # 2 of 3 attempts passed: P(both of 2 sampled attempts pass) = C(2,2)/C(3,2) = 1/3
    assert pass_hat_k(3, 2, 2) == pytest.approx(1 / 3)


def test_mcnemar() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(0, 10) < 0.01
    assert mcnemar_exact(3, 10) == mcnemar_exact(10, 3)


def test_wilson_known_values() -> None:
    seven = wilson_ci(7, 7)
    assert seven.method == "wilson" and seven.mean == 1.0 and seven.n == 7
    assert seven.low == pytest.approx(0.646, abs=1e-3) and seven.high == 1.0
    three = wilson_ci(3, 10)
    assert three.low == pytest.approx(0.108, abs=1e-3)
    assert three.high == pytest.approx(0.603, abs=1e-3)


def test_wilson_is_symmetric() -> None:
    a, b = wilson_ci(2, 9), wilson_ci(7, 9)
    assert a.low == pytest.approx(1 - b.high) and a.high == pytest.approx(1 - b.low)


@given(st.integers(1, 500).flatmap(lambda n: st.tuples(st.just(n), st.integers(0, n))))
def test_wilson_brackets_the_proportion_with_width(nk: tuple[int, int]) -> None:
    n, k = nk
    est = wilson_ci(k, n)
    assert 0.0 <= est.low <= k / n <= est.high <= 1.0
    assert est.low < est.high  # never zero width, not even at 0/n or n/n


def test_wilson_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        wilson_ci(5, 4)
    with pytest.raises(ValueError):
        wilson_ci(0, 0)
