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

## Amendment (2026-10-10): evidence as groups of alternatives

`Gold.evidence` is a list of groups of URLs. Every group must be read or cited; any URL inside a
group satisfies it. A flat list is accepted as shorthand for one group per URL, which keeps the
old meaning (all required). Reason: with 333 pages the same fact is often stated on several
pages (a service guide, its quotas page, the General Reference), and `evidence_recall` must not
penalize the agent for grounding an answer on one of them rather than another. Case authors list
every page that states the fact. Smoke case 005 was the first example: the Lambda timeout
appears on three pages.

## Amendment (2026-10-11): paraphrase groups never enter test; the split is frozen

`stratified_split` pins every paraphrase group (the source case and its paraphrases) to dev, the
same way it pins `hold` cases. Two reasons: a paraphrase in dev of a question in test would leak
that question to anyone tuning on dev, and paraphrase consistency is measured within one run, so
the group has to be scored together. Robustness is therefore a dev-split measurement; the test
split measures the headline numbers.

The split ran once, on 2026-10-11, over the 280 cases Aleena verified (249 in the five original
draft files plus 31 changed-default cases), all drafted synthetically with verbatim support
quotes and none rejected:

```
evals split suites/docs/verified.jsonl --dev 130 --test 150 --seed 20261011
```

Result: `suites/docs/dev.jsonl`, 130 cases (lookup 74, changed_default 20, multi_step 12,
false_premise 12, unanswerable 12; includes the 2 held cases and the 5 paraphrase groups, 20
cases). `suites/docs/test.jsonl`, 150 cases (lookup 72, changed_default 26, false_premise 19,
multi_step 17, unanswerable 16). The seed is the date, chosen before the command ran. The draft
files and the merged source were then deleted; git history keeps them. `test.jsonl` is frozen:
nobody reads it to write prompts, lessons, examples or fixes, and new regression cases go to
dev.

