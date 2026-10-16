# Changelog

One entry per gated improvement iteration (phase 5). Every entry states what changed, the
metric deltas on dev and on the frozen test split with their confidence intervals, the dataset
version, and the failure categories the change targeted. Nothing is promoted without an entry.

Format:

```
## vN (YYYY-MM-DD) - one line on what changed

- Change: prompt vX / tool description / lessons / validator ... (commit SHA)
- Dataset: suites/docs/dev.jsonl vA (+k regression cases from category C), test vB frozen
- Dev:  correct 0.xx -> 0.yy (95% CI of the difference +a to +b), hallucination 0.xx -> 0.yy
- Test: correct 0.xx -> 0.yy (95% CI +a to +b), McNemar p = ..., no category regressed > t
- Cost per success $x -> $y, p95 latency ...
- Runs: baseline <run id>, candidate <run id>
```

## Rejected: iteration 2 (2026-10-16) - number coverage check at submit time

- Change: `require_number_coverage` (`configs/challenger-numbers.yaml`, commit d181715): a
  submission whose answer states a number found in neither the question nor any cited quote is
  rejected with the numbers named, so the model quotes or drops them. One change, on top of v3.
- Targeted: the remaining 69% of answers with an unsupported claim; unsupported numbers in
  particular (`unsupported_entity` 0.034 on v3).
- Dev, paired against v3 (`results/gates/iteration-2-dev-gate.txt`, run
  `docs-20261016-225705-5447b9`, 1560 of 1597 agent calls from cache, so about $0.05):
  judge_supported 0.308 -> 0.328 (+0.020, 95% CI +0.000 to +0.045, no detectable change);
  unsupported_entity 0.034 -> **0.000** (the check does exactly what it says);
  rejected_submissions 0.021 -> 0.118 (**regressed**: the check fired on 38 of 390 attempts);
  correct 0.959 -> 0.951 (no detectable change); one new `malformed_submission` where the
  agent could not satisfy the check because the number was legitimate arithmetic (50 x 65,536).
- Gate: **failed**. The target did not move with its interval, and the mechanism's own cost
  showed up as a regression. Reading: numbers are a small share of the unsupported claims; the
  bulk is unquoted prose context, which a number check cannot touch. The deterministic metric
  it was aimed at went to zero, which is a useful finding in itself, not a shipped change.
- Decision: **not shipped**. v3 stays the champion. Candidate for a later iteration: sentence-level
  coverage (each sentence must share a distinctive phrase with a cited quote), or asking the
  model to tag each sentence with the citation that backs it so the check is structural.
- Runs: champion `docs-20261016-214710-02abb7`, challenger `docs-20261016-225705-5447b9`.

## v3 (2026-10-16) - iteration 1: cite every sentence, cite table headers

- Change: `docs_agent.prompt_version` v2 -> v3 (`configs/challenger-v3.yaml`; commit 68064d7).
  One change: every factual sentence must be backed by a cited quote, context included; prefer a
  shorter fully-quoted answer; cite a table's header row beside any row.
- Targeted: failure taxonomy "unsupported context" (judge fully-supported 11% at baseline) and
  headerless table rows.
- Dataset: `suites/docs/dev.jsonl` v1 (130 cases, 3 attempts), test v1 frozen (150).
- Dev, paired on the same cases (`results/gates/iteration-1-dev.txt`):
  judge_supported **0.114 -> 0.306** (+0.192, 95% CI +0.141 to +0.245, McNemar p = 0.0004);
  judge_core_supported 0.893 -> 0.939 (+0.046, CI +0.006 to +0.088);
  correct 0.918 -> 0.900 (-0.018, CI -0.051 to +0.013, no detectable change);
  wrong_abstention 0.031 -> 0.017 (improved); fabricated_citation 0.021 -> 0.013,
  unsupported_entity 0.046 -> 0.034 (no detectable change); flip rate 0.100 both.
  Cost per success $0.0027 -> $0.0028; p95 latency 17.3 s -> 15.2 s.
- Gate (`results/gates/iteration-1-dev-gate.txt`): **passed**. Under the first wording of
  condition 3 (flat flip-rate band) it had tripped on storage, 4 attempt flips of 36; reading them
  showed 3 keyword-grader misses the judge contradicted and 1 markup rejection. Aleena approved
  widening the keyword lists of 20 dev cases (page or agent wording, gold unchanged, test cases
  untouched); both runs were regraded for free. Correct then read 0.954 -> 0.959 and the flip
  rate fell from 0.100 to 0.031, so most of the measured noise had been the grader. Condition 3
  was rewritten as a paired per-category interval (ADR 0008 records the change and its timing):
  no category regressed, lookup (+0.023) and ai_ml (+0.035) improved.
- Test, run once (`docs-20261016-222256-04f0f4` vs v2 `docs-20261011-225715-b67388`, both judged
  by `2cf75fa03e09`): judge_supported **0.106 -> 0.318** (+0.212, 95% CI +0.136 to +0.295,
  p = 8e-7); correct 0.987 -> 0.980 (-0.007, CI -0.033 to +0.013, no detectable change);
  unsupported_entity 0.060 -> 0.030 (improved); fabricated_citation 0.028 -> 0.028;
  judge_core_supported 0.909 -> 0.939 (no detectable change); tool_errors 0.27 -> 0.19 per
  attempt; p95 latency 14.7 s -> 13.2 s; $0.34.
- Cost of knowing: dev run $0.99, dev judge ~700 Sonnet calls, test run $0.34, two test judge
  passes ~530 calls. Reliability: v3's dev flip rate 0.069 vs v2's 0.031 after regrade; pass^3
  0.915 vs 0.938. Recorded as the one cost of the change.
- Decision: **shipped**. `prompt_version: v3` in `configs/default.yaml`; v2 stays selectable.
  Test-split exposure: 2 runs (v2 baseline, v3 once).
- Runs: champion `docs-20261011-223111-702e87`, challenger `docs-20261016-214710-02abb7`.

## Unreleased

- Phase 1 and phase 2 scaffolding; no gated iteration yet.
- 2026-10-11: reliability and grounding metrics (ADR 0007): flip rate, pass@k, `repeated_calls`,
  `evidence_precision`, `unsupported_entity`. Trajectory grader v3, new grounding grader. No
  run yet uses them.
