# ADR 0006: Case provenance, verification and split assignment

**Status:** accepted (2026-10-10)

## Context

Phase 2 needs about 250 cases. Writing them by hand is slow; having an agent draft them is fast
but risks exactly the failure the project measures: confident, wrong, or unsupported claims
turning into gold answers. The eval-integrity rules in AGENTS.md already say drafts are
synthetic and a human verifies them. This ADR makes those rules mechanical.

## Decision

- `Case` gains `area` (the service area, for the case mix and per-area reporting),
  `verified_by` (who checked the case against its page), and `Gold.support` (the sentence on an
  evidence page the answer rests on, copied word for word). `split` gains the value
  `unassigned`.
- A synthetic case without `verified_by` is a **draft**. Drafts live in `*.drafts.jsonl` with
  `split: "unassigned"`. `evals run` refuses to score drafts or unassigned cases unless
  `--allow-drafts` is passed, which is for throwaway runs only.
- `evals validate`, when a snapshot is present, errors on an evidence URL that is not in the
  snapshot and on a `support` quote that does not appear on an evidence page, and warns when
  none of a case's `must_include` phrases appears on its evidence pages or when `area` is
  missing. A drafted case with an invented quote therefore fails validation before anyone
  reads it.
- `evals split SOURCE --dev N --test M --seed S` assigns verified cases to dev and test at random
  within each (type, area) stratum, so both splits share the mix, and writes `dev.jsonl` and
  `test.jsonl`. The seed is recorded in HANDOFF.md. Nobody hand-picks which cases become test.
- Verification is a human step: read the question, the gold answer and the support quote
  against the page, then set `verified_by`. Fix a gold answer only if the page says otherwise,
  never to make the agent pass.

## Consequences

- Drafting can be fast and parallel; the slow step is a two-minute read per case, which is the
  right place for a human.
- The `support` quote doubles as the reviewer's pointer and as a machine check, so an invented
  fact needs an invented quote to pass, and the invented quote fails validation.
- Test cases are never consulted for prompt, lesson or example work; the split tool and the
  run guard make that a mechanical property rather than a habit.
