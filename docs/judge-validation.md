# Judge validation

**Status:** scored, 2026-10-16. Fully supported: kappa 1.00 on 65 blind judge-test labels (validated).
Core supported: kappa 0.37 (not validated; advisory). Responsive: 0.68 to 0.84 on closed-book labels.
Probes passed. Judge `2cf75fa03e09`.
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

## 2b. Second round: judge-dev relabeled under the two-question rubric (2026-10-13)

Aleena relabeled the 49 judge-dev answers with both questions; the judge (`8251be566ff1`,
`JUDGE_SUPPORTED_V2`) was re-run over the dev run. Judge-test is untouched and unread.

| Verdict | n | kappa | TPR | TNR | tp/fp/fn/tn |
|---|---|---|---|---|---|
| core supported | 49 | 0.395 | 0.67 (0.21 to 0.94) | 0.91 (0.80 to 0.97) | 2/4/1/42 |
| fully supported | 49 | 0.137 | 1.00 (0.87 to 1.00) | 0.13 (0.05 to 0.32) | 26/20/0/3 |
| responsive | 49 | 1.0 (degenerate) | undefined | 1.00 | 0/0/0/49 |

Raw agreement on core is 44 of 49; kappa is low because Aleena failed only 3 answers, so each
disagreement weighs heavily. Reading the disagreements, judge-dev only:

- **Core, 5 disagreements.** Four are the rubric's headerless-table-row rule: a row like
  "Cost | Standard pricing | Approximately 10% savings" or "Elasticsearch 7.10 | November 7, 2027 |
  Not announced | November 7, 2030" quoted without its header. The judge applied the rule all four
  times; the labels passed three of them and failed one (`docs-core-002`, where the judge passed
  because a second quote says the table lists deprecation dates). The fifth (`docs-mix-012`) is a
  label slip: the note says "not adjustable" is quoted, and it is not in the cited quotes.
- **Fully, 20 disagreements, all judge fail / human pass.** In every one the judge names a
  specific claim that is in the answer and in no quote: "each document is at most 5 KB", "an email
  one month before each upgrade", "request an increase through the Service Quotas console",
  "SnapStart is available for Java 11/17". Eighteen of the twenty carry no note. By the rubric
  the judge is right in all twenty.

Conclusion: no prompt change is indicated on judge-dev. The judge applies the written rubric;
the labels do not yet apply it consistently, on the strict question especially. Calibration step:
Aleena revisits judge-dev with the judge's verdicts and reasons shown (`evals label --revisit
all --split judge-dev --show-judge`, a flag the tool refuses on judge-test), keeps or changes each
label on the merits, and decides the open rubric point below. Judge-test is then labeled fresh,
without the judge in view, and scored once.

Rubric decision (Aleena, 2026-10-16): the headerless-row rule **stays strict**. A table row or
cell quoted without its header proves only what it literally says; which column a value belongs
to cannot be inferred. This binds the labels as it already binds the judge, so the three judge-dev
core passes on headerless rows (`docs-ai-004-p1`, `docs-chg-013`, `docs-chg-030`) are expected to
flip to "no" in the calibration pass, and the agent-side fix, quote the header row with the cell,
goes on the improvement-loop list.

## 2c. Calibration pass and the closed-book labels (2026-10-16)

Aleena revisited all 49 judge-dev labels with the judge's verdicts in view (`--show-judge`,
refused on judge-test), changed what the rubric required, and labeled 41 closed-book answers
(run `closed-book-20261011-225242-b58341`, same model with no tools, 54.6% correct) for the
responsive question. The judge was then run over the closed-book answers (98 calls).

| Run | Verdict | n | kappa | TPR | TNR | tp/fp/fn/tn |
|---|---|---|---|---|---|---|
| dev, judge-dev | core supported | 49 | **0.693** | 0.80 (0.38 to 0.96) | 0.95 (0.85 to 0.99) | 4/2/1/42 |
| dev, judge-dev | fully supported | 49 | 0.219 | 1.00 (0.89 to 1.00) | 0.18 (0.06 to 0.37) | 32/14/0/3 |
| closed-book, judge-dev | responsive | 17 | 0.850 | 0.80 (0.38 to 0.96) | 1.00 (0.76 to 1.00) | 4/0/1/12 |
| closed-book, judge-test | responsive | 24 | **0.700** | 0.75 (0.30 to 0.95) | 0.95 (0.76 to 0.99) | 3/1/1/19 |
| closed-book, all | responsive | 41 | 0.778 | 0.78 (0.45 to 0.94) | 0.97 (0.84 to 0.99) | 7/1/2/31 |

Reading, judge-dev only:

- **Core**: 46 of 49 agree. Two of the three disagreements (`docs-chg-013`, `docs-chg-030`) are
  headerless rows kept as "core = yes" after the rule was confirmed strict; under that rule they
  are "no". The third (`docs-core-002`) is the judge passing a headerless row because a second
  quote says the table lists deprecation dates, which the labeler counts as not enough. A
  defensible disagreement either way; it stays.
- **Fully**: 14 disagreements remain, every one a judge fail / human pass with no note. In each
  the judge names a sentence that is in the answer and in no quote ("each document is at most 5
  KB", "an email one month before each upgrade", "SnapStart is available for Java 11/17"). These
  read as labels kept with Enter rather than disputed on the merits, so a short pass over exactly
  the disputed labels (`--revisit disagree --show-judge`) is the next step, with a note wherever
  the judge is overruled.
- **Responsive** has its first real negatives: 9 of 41 closed-book answers were not responsive.
  The judge caught 7 of 9 and wrongly failed 1 of 32 good answers. On the 24 judge-test labels
  kappa is exactly 0.70 with wide intervals; 20 more closed-book labels would make the number
  worth quoting. Two closed-book labels were corrected by hand after an off-by-one at the start of
  the session (the first answer's note had landed on the second answer, and the second's verdict
  had followed); both corrections are recorded in the exported file.

## 2d. Decisions before the final scoring (Aleena, 2026-10-16)

1. The strict headerless-row rule applies everywhere, refined: a quoted header fragment naming
   the column counts as the header; prose near the row does not. `docs-chg-013` and
   `docs-chg-030` flipped to core = no (neither quote set names the date column). The rubric
   text, read by the judge and shown on the label screen, carries the refinement; the judge was
   re-run with it.
2. The 14 strict disputes: Aleena agreed that a factual sentence no quote states fails "fully
   supported", re-checked each against its quotes, and flipped those where the judge was right.
   Any kept are listed with the quote that supports them.
3. Then the 65 judge-test answers are labeled blind, once, and scored once.

## 2e. Judge-dev, final (2026-10-16)

The remaining strict disputes turned out to be a screen problem: the label screen had shown each
quote inside 200 characters of page context, and the notes on 13 kept labels cited header rows
and neighbouring rows that were visible there but absent from the stored citations the judge
sees. The screen now shows the citations first, alone and verbatim, and dims the context as
uncited. Aleena redid the disputed pass on the fixed screen. Judge `2cf75fa03e09` (final rubric).

| Verdict | n | kappa | TPR | TNR | tp/fp/fn/tn |
|---|---|---|---|---|---|
| core supported | 49 | **0.787** | 0.88 (0.53 to 0.98) | 0.95 (0.84 to 0.99) | 7/2/1/39 |
| fully supported | 49 | **1.000** | 1.00 (0.92 to 1.00) | 1.00 (0.44 to 1.00) | 46/0/0/3 |
| responsive | 49 | degenerate | no failures on dev | 1.00 | 0/0/0/49 |

Three disagreements remain on core, all reasoned and recorded: `docs-int-002` and `docs-mix-049`
(Aleena: the core claim is in quote 1, the judge: the claim leans on which workflow type or on
"remove" versus "add"), and `docs-mix-056` (Aleena: the cited row does not say 100 is the
default; the judge passes it). These are the kind of disagreement a judge is allowed to have.

Both support bars are met on judge-dev. Judge-dev was used for tuning (two rubric revisions, one
screen fix, two label calibration passes), so these numbers are not the published ones. The
published numbers come from the 65 judge-test answers, labeled once, fresh, on the fixed screen,
without the judge in view, then judged once with the final judge.

## 2f. Published result: judge-test, labeled blind, scored once (2026-10-16)

Aleena relabeled the 65 judge-test answers fresh on the fixed screen (`--revisit all --split
judge-test --fresh`: no previous answers shown, no judge in view, two-question rubric, strict
headerless-row rule, a note on 58 of 65). The final judge (`2cf75fa03e09`, `JUDGE_SUPPORTED_V2`
with the refined rubric) then graded those 65 answers, 130 calls, and `judge-validate` ran once.

| Verdict | n | kappa | TPR (judge catches her fails) | TNR (judge passes her passes) | tp/fp/fn/tn | Bar 0.7 |
|---|---|---|---|---|---|---|
| **fully supported** | 65 | **1.000** | 1.00 (0.94 to 1.00) | 1.00 (0.65 to 1.00) | 58/0/0/7 | **met** |
| core supported | 65 | **0.366** | 0.33 (0.12 to 0.65) | 0.96 (0.88 to 0.99) | 3/2/6/54 | **missed** |
| responsive (dev run) | 65 | degenerate | no human failures | 1.00 | 0/0/0/65 | see closed-book |
| responsive (closed-book, judge-test) | 36 | 0.679 | 0.80 (0.38 to 0.96) | 0.94 (0.79 to 0.99) | 4/2/1/29 | just missed |
| responsive (closed-book, all 61) | 61 | 0.839 | 0.91 (0.62 to 0.98) | 0.96 (0.87 to 0.99) | 10/2/1/48 | |

**Fully supported is validated.** On 65 blind labels the judge and the labeler agree on every
answer: 58 answers carry at least one claim no quote states, 7 do not. This is the brief's
claim-level unsupported rate, and it is the hallucination number this project reports.

**Core supported is not validated**, and the post-score reading of the eight disagreements says
why (read after the single scoring; it may inform the next judge version, which must then be
validated on new labels, never on these 65):

- Six are human fail / judge pass, and five of those are the same pattern: the labeler applies the
  strict row rule to the core figure itself. "Cache behaviors per distribution | 75" states a
  number but not that 75 is the *default*; "Text response length | 4,000 | No | N/A" does not say
  4,000 is a *maximum*; "IPv4 CIDR blocks per VPC | 5 | [Yes] (up to 50)" does not establish 5 as
  the default without its header. The judge accepts a quota row as establishing the number and its
  role. The sixth (`docs-mix-003`) is a plain judge miss: the 300-association figure the answer
  rests on is in no quote.
- Two are human pass / judge fail (`docs-ai-023`, `docs-core-001`): the judge counts billing
  factors and runtime lists as core claims; the labeler counts them as peripheral.
- Agreement is 57 of 65; kappa is low because core failures are rare (9 of 65) and the judge
  caught 3 of the 9.

The judge-dev result for core (0.79) did not carry to judge-test (0.37): the dev tuning fixed the
screen and the rubric, not the judge's reading of quota rows, and judge-dev had few such rows.

**Decisions.**

1. `judge_supported` (fully supported) ships as the validated hallucination metric.
2. `judge_core_supported` is reported as advisory, with this table beside it. A next version of the
   support prompt should add the quota-row example ("a row 'X | 75' states 75, not that 75 is a
   default or a maximum") and the core/peripheral line for billing details; it is validated only
   against labels on a new run's answers.
3. `judge_responsive` is validated on closed-book answers at 0.68 (n = 36) to 0.84 (n = 61); on the
   agent's own answers it has met no failures. Reported with those numbers.

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
