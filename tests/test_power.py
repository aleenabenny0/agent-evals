from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_evals.stats.core import paired_bootstrap_diff
from agent_evals.stats.power import cases_for_paired_delta, detectable_delta, paired_power


def test_known_sample_sizes() -> None:
    # z = 1.960 + 0.842 = 2.802; n = ceil(z^2 * d / delta^2)
    assert cases_for_paired_delta(0.10, 0.15) == 118
    assert cases_for_paired_delta(0.10, 0.20) == 157
    assert cases_for_paired_delta(0.10, 0.30) == 236


def test_round_trip_and_monotonicity() -> None:
    n = cases_for_paired_delta(0.10, 0.20)
    assert detectable_delta(n, 0.20) <= 0.10 < detectable_delta(n - 1, 0.20)
    # Smaller changes and noisier (more discordant) comparisons both need more cases.
    assert cases_for_paired_delta(0.05, 0.20) > n > cases_for_paired_delta(0.20, 0.20)
    assert cases_for_paired_delta(0.10, 0.30) > n > cases_for_paired_delta(0.10, 0.10)


def test_power_at_the_computed_size_is_the_requested_power() -> None:
    n = cases_for_paired_delta(0.10, 0.20, power=0.8)
    assert 0.8 <= paired_power(n, 0.10, 0.20) < 0.82  # ceil rounds n up a little


def test_rejects_impossible_inputs() -> None:
    with pytest.raises(ValueError):
        cases_for_paired_delta(0.10, 0.05)  # fewer disagreeing cases than the net change
    with pytest.raises(ValueError):
        detectable_delta(0, 0.2)
    with pytest.raises(ValueError):
        paired_power(10, 0.0, 0.2)


@given(delta=st.floats(0.02, 0.5), extra=st.floats(0.0, 0.5))
def test_more_cases_detect_smaller_changes(delta: float, extra: float) -> None:
    d = min(1.0, delta + extra)
    n = cases_for_paired_delta(delta, d)
    assert detectable_delta(n, d) <= delta + 1e-12
    assert detectable_delta(2 * n, d) < detectable_delta(n, d)


def test_simulation_agrees_with_the_formula() -> None:
    """The closed form must describe what our actual comparison code does."""
    delta, d = 0.10, 0.20
    n = cases_for_paired_delta(delta, d)
    rng = np.random.default_rng(0)
    only_candidate = (d + delta) / 2  # share of cases only the candidate passes
    sims, detected = 200, 0
    for i in range(sims):
        u = rng.random(n)
        # +1: only the candidate passed; -1: only the baseline passed; 0: both agreed.
        diffs = np.where(u < only_candidate, 1.0, np.where(u < d, -1.0, 0.0))
        est = paired_bootstrap_diff([0.0] * n, diffs.tolist(), n_resamples=400, seed=i)
        detected += int(est.low > 0)
    assert abs(detected / sims - paired_power(n, delta, d)) < 0.1
