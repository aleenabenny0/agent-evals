# ADR 0004: Confidence intervals: Wilson for proportions, bootstrap for everything else

**Status:** accepted (2026-10-10)

## Context

Every reported number carries a 95% confidence interval (design principle 1, and the eval
integrity rule "report results with their confidence intervals"). The percentile bootstrap over
cases was the only method. It fails in the most common early-project situation: when every case
agrees, every resample agrees too, and the interval collapses to zero width. The first smoke run
scored 7/7 on `correct` and reported 1.000 to 1.000, which overstates certainty. A true pass
rate of 65% produces 7 straight passes about 5% of the time, so 65% is still plausible.

## Decision

- `stats.core.wilson_ci(successes, n)` implements the Wilson score interval. `Estimate` carries
  a `method` field ("wilson" or "bootstrap") that the report table shows in an "interval" column.
- `report.summarize` uses Wilson when a metric's per-case values are all exactly 0 or 1, that is,
  when the metric is a proportion of cases. Everything else keeps the bootstrap: counts
  (`model_calls`, `tool_errors`, `rejected_submissions`), fractions (`citation_valid_rate`,
  multi-page `evidence_recall`), and any metric once there is more than one attempt per case,
  because per-case means are then no longer 0 or 1.
- Paired comparisons (`report.compare`) keep the paired bootstrap on per-case differences.
- The choice is made from the data, not from a declared list, so a new binary metric gets the
  right interval without registration, and the table always says which method was used.

## Consequences

- Small suites report honest uncertainty: 7/7 reads 0.646 to 1.000, 4/4 reads 0.510 to 1.000.
- At the ~250 cases planned for phase 2 the two methods nearly coincide for proportions.
- A metric can switch method between runs if its values stop being 0/1 (for example with
  attempts > 1). That is visible in the "interval" column and is correct, not a bug.
- The Wald interval, p ± z·sqrt(p(1-p)/n), was rejected: it has zero width at 0 or 1 and can
  leave [0, 1].
