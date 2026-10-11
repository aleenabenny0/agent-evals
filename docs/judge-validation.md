# Judge validation

**Status:** in progress, 2026-10-11. The judge does not ship until every bar below is met on
judge-test labels. Plan and bars: `docs/plans/llm-judge.md`. Mechanics: `graders/judge.py`.

| Item | Value |
|---|---|
| Judge | `b23a6c3aef09` = claude-sonnet-5-5, `JUDGE_SUPPORTED_V1`, `JUDGE_RESPONSIVE_V1`, strict `submit_verdict` tool, max_tokens 2048 |
| Run judged | `docs-20261011-223111-702e87` (docs agent, dev, 130 cases, 3 attempts) |
| Answers judged | 346 of 390 attempts (44 abstentions skipped; no errored attempts) |
| Calls | 692 (6 from cache), 722,515 input / 161,334 output tokens |
| `judge_responsive` | 0.997 (0.991 to 1.000), 117 cases |
| `judge_supported` | 0.177 (0.123 to 0.234), 117 cases |

## 1. Agreement with the code graders (no labels needed)

| Code says | Judge says | Attempts | What they are |
|---|---|---|---|
| `correct = 0` | responsive pass | 21 | 13 keyword-grader misses, 3 case-defect answers, 3 "no such date" answers to an unanswerable, 2 inferred-but-right answers (failure taxonomy A, C, D) |
| `correct = 0` | not judged | 11 | hedged abstentions (`abstained: true`), skipped by design |
| `correct = 1` | responsive fail | 1 | `docs-core-028` #3: confirmed one half of a two-part question and hedged the other |
| `correct = 1` | supported fail | 267 | core claim supported; added context not in the cited quotes |

Reading: on this run the "responsive" verdict and the keyword grader agree except where the
taxonomy already says the keyword grader is wrong, which is the behaviour a judge should have.
The "supported" verdict is far stricter than the citation grader: it fails an answer for any
sentence the quotes do not carry, and the agent routinely adds a sentence of context from pages it
read but did not quote. Whether that strictness matches a human reader is the question for step 2.

## 2. Agreement with hand labels: pending

Aleena labels dev answers with `evals label docs-20261011-223111-702e87 --labeler aleena`. Labels
are split judge-dev / judge-test by case-id hash before any verdict is seen. Then:

```
uv run evals judge-validate docs-20261011-223111-702e87 --split judge-test
```

Bars: Cohen's kappa at least 0.7 on each verdict; TPR and TNR reported with Wilson intervals and
the confusion counts; at least 30 labeled answers before any conclusion. If "supported" misses
the bar because the labels pass answers the judge fails, the prompt is revised on judge-dev
(for example: judge the claims that answer the question, and name added context separately)
and judge-test is scored once more, once.

## 3. Mutation tests and bias probes

Variants are built from answers the judge passed on both verdicts (40 of them here), judged, and
compared with the plan's bars. Every miss, with the judge's own reasons, is in
`results/judge-probes-<run>.json`.

**Round one** (mutants: change a number, drop a qualifier, swap the first citation):

| Variant | Kind | n | Expected | Observed (95% CI) | Bar | Met |
|---|---|---|---|---|---|---|
| number | mutant | 15 | judge fails it | 1.00 (0.80 to 1.00) | 0.90 | yes |
| qualifier (dropped) | mutant | 11 | judge fails it | 0.00 (0.00 to 0.26) | 0.90 | **no** |
| citation (first swapped) | mutant | 40 | judge fails it | 0.93 (0.80 to 0.97) | 0.90 | yes, barely |
| padding | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |
| resample | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |

292 judge calls, 288k input / 62k output tokens.

Reading the eleven qualifier misses showed a flaw in the mutant, not the judge: dropping
"approximately" from "approximately 10% cheaper", or "additional" from "does not add an
additional fee", leaves a true sentence, and the judge was right to keep passing it (its reason
for one: "a slight tightening of approximately, but it later describes the 10% as an
approximation"). The three citation misses were answers with two citations where the untouched
second one still carried the claim. Both mutants were redesigned before round two: a bound is
now flipped to its contradiction ("up to" becomes "at least", "by default" becomes "always",
"approximately" becomes "exactly"), and every citation is swapped. The padding and resample
misses (two each) are the same two answers whose "supported" verdict sits on the edge; the
re-sample moved them without any change to the text, so that is the judge's own noise, 5% here.

**Round two** (mutants: change a number, flip a bound, swap every citation):

| Variant | Kind | n | Expected | Observed (95% CI) | Bar | Met |
|---|---|---|---|---|---|---|
| number | mutant | 15 | judge fails it | 1.00 (0.80 to 1.00) | 0.90 | yes |
| qualifier (bound flipped) | mutant | 11 | judge fails it | 0.82 (0.52 to 0.95) | 0.90 | **no**, interval includes it |
| citation (all swapped) | mutant | 40 | judge fails it | 1.00 (0.91 to 1.00) | 0.90 | yes |
| padding | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |
| resample | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |

292 judge calls, 244 of them from cache (only the changed variants cost anything).

The two qualifier misses split one each way. `docs-chg-004` #2: "by default" became "always",
which keeps the sense of the sentence, so the judge was right again and the flip table was
weak; "by default" now flips to "only on request". `docs-int-002` #2: "up to 90 days" became "at
least 90 days" against a quote that says "up to 90 days", and the judge passed it: a real miss,
the first one attributable to the judge rather than to the mutant. The four probe misses are the
same three answers as in round one, whose "supported" verdict sits on the edge and moves between
samples; that is the judge's own noise, about 5% of answers, and it is why the plan reports
stability as a number instead of assuming it.

Round three, with the stronger "by default" flip and the same 40 answers, follows below.

PROBE_ROUND_THREE_PLACEHOLDER

## 4. Decision

Pending steps 2 and 3.
