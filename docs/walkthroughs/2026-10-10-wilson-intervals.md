# Walkthrough: Wilson score intervals

**Summary.** The report switched from a pure bootstrap to Wilson score intervals for pass/fail
metrics, because the bootstrap collapses when every case agrees: at 7 of 7 it gives 1.000 to
1.000, while Wilson gives 0.646 to 1.000. This walkthrough explains the math behind the formula,
works that example by hand, and shows where it lives: `wilson_ci` in
`src/agent_evals/stats/core.py`, chosen by `_interval` in `src/agent_evals/report.py`. The
method is picked from the data, so a metric whose per-case values are all exactly 0 or 1 gets
Wilson and any other metric keeps the bootstrap; `compare` still uses the paired bootstrap. The
decision record is ADR 0004.

Why the report switched from a pure bootstrap to Wilson intervals for pass/fail metrics, the
math behind the formula, and where it lives in the code. Decision record: ADR 0004.

## 1. The problem

A **confidence interval** (CI) is the range of true values that could plausibly have produced
the result you observed. The report uses 95% intervals.

The **bootstrap** estimates that range by resampling: draw the cases with replacement many
times, compute the mean each time, and take the 2.5th and 97.5th percentiles. It is general and
needs no formula, which is why it is the right tool for counts, fractions and paired differences.

It has one blind spot. When every case agrees, every resample agrees too. At 7 of 7 the mean of
every resample is 1.0, so the interval is 1.000 to 1.000. That says "certain", and it is wrong:
if the true pass rate were 65%, seven straight passes would still happen about 5% of the time
(0.65 to the power 7 is 0.049).

## 2. The Wilson score interval

Wilson asks the question directly: for which true rates p would k passes out of n not be
surprising at the 95% level? Solving that inequality for p gives a closed form. With
z = 1.96 (the normal quantile for 95%) and p̂ = k/n:

```
centre     = (p̂ + z²/2n) / (1 + z²/n)
half-width = z / (1 + z²/n) × sqrt( p̂(1−p̂)/n + z²/4n² )
interval   = centre ± half-width
```

Worked example, 7 of 7 (p̂ = 1, n = 7, z² = 3.8416):

```
1 + z²/n   = 1 + 0.5488 = 1.5488
centre     = (1 + 0.2744) / 1.5488 = 0.8229
half-width = 1.96 / 1.5488 × sqrt(0 + 3.8416 / 196) = 1.2655 × 0.1400 = 0.1772
interval   = 0.646 to 1.000
```

The intuition: Wilson behaves as if about z² ≈ 4 imaginary cases were added, half passes and
half fails, before computing a normal interval. With 7 real cases that pull toward 0.5 is
large. With 250 cases it is negligible, and Wilson and the bootstrap agree closely.

Two properties that matter and that the tests check:

- The interval always contains p̂ and always has positive width, even at 0/n and n/n.
- It never leaves 0 to 1. The textbook "Wald" interval, p̂ ± z·sqrt(p̂(1−p̂)/n), fails both:
  zero width at p̂ = 0 or 1, and it can run past 100%.

## 3. Where it lives

`src/agent_evals/stats/core.py`, `wilson_ci(successes, n)`:

- `z = NormalDist().inv_cdf(0.5 + confidence / 2)` gets 1.96 from the standard library, no new
  dependency. `inv_cdf` is the inverse of the normal cumulative distribution function.
- The three lines after it are the formula above, with `denom = 1 + z²/n` computed once.
- The ends are set to exactly 0.0 or 1.0 when p̂ is, so floating-point rounding cannot leave
  the interval a hair short of the observed value.
- `mean` on the returned `Estimate` is the observed proportion, not the Wilson centre. The
  table's "mean" column keeps meaning what it says.

`Estimate` gained `method`, defaulting to `"bootstrap"`, so every existing caller is unchanged.

`src/agent_evals/report.py`, `_interval(case_means)`: if every per-case value is exactly 0.0 or
1.0, the metric is a proportion and gets Wilson; otherwise it gets the bootstrap. That rule is
data-driven on purpose: a new binary metric is handled without registering it anywhere, and a
metric whose values stop being 0/1 (for example once there are two attempts per case, so a case
can score 0.5) falls back to the bootstrap automatically. The CLI table shows the method in an
"interval" column so a reader never has to guess.

`compare` is untouched. Paired differences are not proportions, and the paired bootstrap is the
right tool there.

## 4. Tests

`tests/test_stats.py`:

- Known values: 7/7 gives 0.646 to 1.000; 3/10 gives 0.108 to 0.603 (a textbook example).
- Symmetry: the interval for k of n mirrors the one for n−k of n.
- A Hypothesis property over every k and n up to 500: `0 ≤ low ≤ k/n ≤ high ≤ 1` and
  `low < high`.
- Impossible inputs (k > n, n = 0) raise.

`tests/test_harness.py`: a run with two attempts per case reports `bootstrap` for `correct`
because per-case means can be 0.5; a single-attempt run where all four cases pass reports
`wilson` with a lower bound near 0.51, where the bootstrap would have said 1.0.

## 5. Questions a reader might ask

**Why not always Wilson?** Wilson is for proportions. `model_calls` is a count, and a per-case
mean over three attempts is not a 0/1 outcome. The bootstrap handles those without assumptions.

**Why not always bootstrap?** Because of the collapse at unanimous results, exactly the situation
a small smoke suite produces. Reporting 1.000 to 1.000 for 7 cases would be a false claim.

**Why pick the method from the data instead of a list of metric names?** Fewer places to
forget. A list would need updating for every new metric, and would be wrong the moment attempts
per case changed. The table shows the method, so nothing is hidden.

**What changes at 250 cases?** Almost nothing visible: both methods give close intervals for
proportions. The change matters now, while the suites are small.
