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

## Unreleased

- Phase 1 and phase 2 scaffolding; no gated iteration yet.
- 2026-10-11: reliability and grounding metrics (ADR 0007): flip rate, pass@k, `repeated_calls`,
  `evidence_precision`, `unsupported_entity`. Trajectory grader v3, new grounding grader. No
  run yet uses them.
