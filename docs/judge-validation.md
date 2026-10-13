# Judge validation

**Status:** in progress, 2026-10-12: probes passed; hand labels in (114); supported kappa 0.26, bar
missed; rubric revision proposed.
The judge does not ship until every bar below is met on judge-test labels. Plan and bars: `docs/plans/llm-judge.md`. Mechanics: `graders/judge.py`.

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

## 2. Agreement with hand labels (2026-10-12)

Aleena labeled all 114 attempt-1 answers of the run (`evals label`), 49 on judge-dev and 65 on
judge-test, assigned by case-id hash before any verdict was seen. Labels are exported to
`results/labels/docs-20261011-223111-702e87.jsonl`. Scoring is `evals judge-validate`; positive
class = a real failure.

| Split | Verdict | n | kappa | TPR (judge catches her fails) | TNR (judge passes her passes) | tp/fp/fn/tn | Bar 0.7 |
|---|---|---|---|---|---|---|---|
| judge-test | supported | 65 | **0.256** | 1.00 (0.88 to 1.00) | 0.28 (0.16 to 0.44) | 29/26/0/10 | **missed** |
| judge-test | responsive | 65 | 1.000 (degenerate) | undefined, no human fails | 1.00 (0.94 to 1.00) | 0/0/0/65 | not testable yet |
| judge-dev | supported | 49 | 0.176 | 1.00 (0.82 to 1.00) | 0.23 (0.11 to 0.40) | 18/24/0/7 | |
| all | supported | 114 | 0.219 | 1.00 (0.92 to 1.00) | 0.25 (0.16 to 0.37) | 47/50/0/17 | |

**Reading, from the judge-dev disagreements only** (the 26 judge-test disagreements were not
read, so judge-test can still be scored once after a revision):

- The judge never passes an answer Aleena failed (TPR 1.00 everywhere). Every disagreement is
  an answer she passed and the judge failed: 24 on judge-dev.
- All 24 are the same shape: the fact that answers the question is quoted, and the answer adds
  context the quotes do not carry ("the quota is adjustable", "check Service Quotas", "a public
  NAT gateway must be in a public subnet", "each document is at most 5 KB", a named API
  parameter). Six of them are table-row quotes with no column header, where a reader who knows
  AWS quota tables sees "Yes" as "adjustable" and the judge, correctly by the letter, does not.
- Her own "no" notes fail answers for exactly that kind of added context (workaround steps,
  API names, tier details not quoted). Across all 114 she split those cases about evenly: 47
  fails, 50 passes. So the written definition ("every factual claim backed by the quotes") is
  being applied consistently by the judge and inconsistently by the labels, with a lenient lean.
  A judge cannot reach kappa 0.7 against a reference that disagrees with itself at that rate; a
  prompt revision alone will not fix this.
- "Responsive" agreed 114 of 114 because neither rater failed anything. Kappa is undefined
  when both raters are constant; the table shows 1.0 only because the code returns 1.0 for
  perfect agreement. The verdict is untested until it meets answers that are not responsive.
- One label needs a look: `docs-ai-002` #1 carries a note about `modelId`, `Converse` and
  `InvokeModel`, which belong to `docs-ai-001`'s answer, not this one. Probably typed at the
  wrong prompt during the revisit; the label itself may be wrong too.

**Decision needed (Aleena):**

1. **Sharpen the rubric and split the verdict.** Two questions instead of one: *core supported*
   (the claims that answer the question are in the quotes) and *fully supported* (every claim
   is). The judge returns both; the brief's claim-level unsupported rate is the second, a usable
   response-level hallucination metric is the first. Relabel judge-dev (49 answers, about 40
   minutes) with the two-question rubric, tune the judge prompt against it, then relabel and
   score judge-test once.
2. **Give "responsive" something to catch.** Label 30 to 40 closed-book answers (run
   `closed-book-20261011-225242-b58341`, 54.6% correct, so roughly half are wrong) with the
   responsive question only, so TPR is measurable.
3. Fix or confirm the `docs-ai-002` #1 label with `evals label ... --revisit all --limit 3`.

Until then the judge's "supported" number is reported as unvalidated, as it is in the README.

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

**Round three** (same 40 answers; "by default" flips to "only on request"; earliest bound in the
text is the one flipped):

| Variant | Kind | n | Expected | Observed (95% CI) | Bar | Met |
|---|---|---|---|---|---|---|
| number | mutant | 15 | judge fails it | 1.00 (0.80 to 1.00) | 0.90 | yes |
| qualifier (bound flipped) | mutant | 11 | judge fails it | 1.00 (0.74 to 1.00) | 0.90 | yes |
| citation (all swapped) | mutant | 40 | judge fails it | 1.00 (0.91 to 1.00) | 0.90 | yes |
| padding | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |
| resample | probe | 40 | verdicts unchanged | 0.95 (0.83 to 0.99) | 0.95 | at the bar |

292 judge calls, 288 from cache. The four probe misses are, again, the same borderline answers.
Caveats that stay on the record: the qualifier class has n = 11, so its interval reaches down to
0.74, and round two showed the judge can pass an "at least" where the quote says "up to". The
mutant set is deterministic text surgery; it does not plant subtle errors (a wrong service name,
a swapped Region), and those remain for the hand labels and for later rounds.

## 4. Decision

- Hand-label agreement: **supported missed the bar (kappa 0.26 on judge-test)**; the cause is a
  rubric that the judge applies literally and the labels apply leniently and unevenly. Next
  step is the two-question rubric above, not a prompt tweak. "Responsive" is untested.
- Mutation tests and bias probes: **passed** on round three, with the caveats above and with the
  first two rounds kept in this document so the revisions are visible.
- Agreement with hand labels: **pending** Aleena's labels (step 2). The judge does not ship, and
  no judge number appears in a headline, until kappa is reported on judge-test.
- What the judge is already allowed to do: flag dev answers where code says fail and the judge
  says responsive, as suspected grader misses for human confirmation (failure taxonomy, A).

## 4. Decision

Pending steps 2 and 3.
