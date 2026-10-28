# ADR 0007: Reliability and grounding metrics

**Status:** accepted (2026-10-11). Approved in one go after the brief alignment
(`docs/plans/brief-alignment.md`).

## Context

Brief 02 lists metrics the suite did not yet report: a noise floor for comparisons (flip rate),
pass@k beside pass^k, loop detection, tool-selection precision, and a deterministic
fabricated-entity rate. Each is small, none calls a model, and all of them read data the store
already keeps. Together they also give the closed-book and oracle baselines something to be
compared on besides `correct`.

## Decision

Five additions, each a few lines with tests.

1. **Flip rate** (`stats.core.flip_rate`, shown by `evals report`). The share of cases whose
   repeated attempts disagree on `correct`, with a Wilson interval. Only cases with at least two
   attempts count; with one attempt per case the line is not printed. Attempts carry distinct
   cache salts (`case_id#attempt`), so they are fresh samples, not cache replays. A difference
   between two runs that sits inside this band is noise, whatever the point estimates say.

2. **pass@k** (`stats.core.pass_at_k`). The unbiased estimate of P(at least one of k attempts
   passes), `1 - C(n-c, k) / C(n, k)`, from the Codex paper (Chen et al., 2021). It is printed
   beside pass^k, which is P(all k pass), `C(c, k) / C(n, k)` (tau-bench). pass@1 and pass^1 are
   both the plain pass rate. The gap between the two at k > 1 is the ability the agent has but
   does not deliver to a user who asks once; pass^k is the number that matters for that user.

3. **Repeated calls** (`TrajectoryGrader`, metric `repeated_calls`, version 3). The number of
   tool calls whose (tool name, input) pair already occurred earlier in the same trajectory.
   Input is compared as JSON with sorted keys, so key order does not matter. `submit_answer`
   counts too: resubmitting a rejected answer unchanged is a loop. This is the brief's loop
   detection; it is a count, so it gets a bootstrap interval and lower is better.

4. **Evidence precision** (`TrajectoryGrader`, metric `evidence_precision`). Of the distinct
   pages the agent read, the share that were gold evidence pages. `evidence_recall` asks whether
   the right pages were found; this asks how much else was read. Omitted when the agent read
   nothing (the oracle baseline is handed its pages), because 0/0 is not a rate. Cited pages are
   not counted as read here, unlike in recall: reading is the action whose precision is measured.

5. **Unsupported entities** (new `GroundingGrader`, metric `unsupported_entity`). Every
   standalone number and every URL in the final answer must appear in the question, on a page
   the agent read, or on a page it cited. Numbers are extracted by one shared regex
   (`agent_evals.entities`) from both sides, with thousands separators removed, and may not touch
   a letter or digit, so "S3", "EC2" and "python3.10" are names, not numbers. The metric is 0/1
   per answer (the brief's fabricated-entity *rate*), with the offending entities in the reason.
   Abstentions and errored attempts get no grade.

The metric is named **unsupported**, not fabricated, on purpose. The check cannot tell an
invented figure from a converted one: a page that says "900 seconds" does not support an answer
that says "15 minutes" unless the page prints both. The judge's "supported" verdict (plan step 6)
settles conversions; this metric catches the invented figures that matter, on every attempt,
for free. It is also reference-free, so the same grader scores live traffic in phase 7.

The same number extraction now backs two review aids. `evals validate` warns when a number in
the gold answer is on no evidence page (a wrong gold answer) or in no support quote (a reviewer
cannot see where it comes from). `evals review` marks every required phrase, number and
digit-bearing identifier from the gold answer in cyan wherever it appears around the yellow
quote, and lists the answer facts no quote covers, so the reviewer sees the whole answer on the
page, not just the first sentence.

## Consequences

- The closed-book baseline will score near 100% on `unsupported_entity` by construction: it
  reads nothing. That is the point of the baseline, and the README will say so.
- A "fabricated action" rate does not apply to this agent: it only reads. The README says so
  instead of reporting a zero.
- `uncited_answer`, `wrong_abstention` and `missed_abstention` are this agent's policy
  violations, in the brief's words; they are not renamed.
- `TrajectoryGrader` is version 3 and the grounding grader is recorded in the run manifest, so a
  report always says which graders produced it.
- Numbers written as words ("five minutes") are not checked. A later version may add a small
  word-to-number table if the judge finds it matters.
