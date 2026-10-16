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

## Iteration 1 (2026-10-16) - system prompt v3: cite every sentence, cite table headers

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
- Gate: conditions 1 and 2 met. Condition 3 (no type or area drops by more than the flip-rate
  band) **tripped on storage** (correct 0.972 -> 0.861, 12 cases, 4 attempt flips of 36). Read:
  3 are `docs-chg-023` answered with the page's wording that the keyword list lacks (judge:
  responsive on all 3), 1 is a tool-markup rejection (`docs-core-042`). 11 cases got worse and
  7 better overall; 20 of the 22 new failing attempts are keyword-grader misses the judge
  contradicts, 2 are agent errors (one max_tokens, one markup leak).
- Decision: pending Aleena (ship with the category flag explained, or iterate). Test: not run.
- Runs: champion `docs-20261011-223111-702e87`, challenger `docs-20261016-214710-02abb7`.

## Unreleased

- Phase 1 and phase 2 scaffolding; no gated iteration yet.
- 2026-10-11: reliability and grounding metrics (ADR 0007): flip rate, pass@k, `repeated_calls`,
  `evidence_precision`, `unsupported_entity`. Trajectory grader v3, new grounding grader. No
  run yet uses them.
