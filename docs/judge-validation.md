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

**Round four, against the final judge `2cf75fa03e09`** (same 40-answer pool; 38 answers now pass
both verdicts under the stricter rubric, so n fell):

| Variant | Kind | n | Expected | Observed (95% CI) | Bar | Met |
|---|---|---|---|---|---|---|
| number | mutant | 15 | judge fails it | 1.00 (0.80 to 1.00) | 0.90 | yes |
| qualifier (bound flipped) | mutant | 5 | judge fails it | 0.80 (0.38 to 0.96) | 0.90 | too few to say (n = 5) |
| citation (all swapped) | mutant | 38 | judge fails it | 1.00 (0.91 to 1.00) | 0.90 | yes |
| padding | probe | 38 | verdicts unchanged | 0.92 (0.79 to 0.97) | 0.95 | **just missed** |
| resample | probe | 38 | verdicts unchanged | 0.84 (0.70 to 0.93) | 0.95 | **missed** |

268 judge calls, 107 from cache. The mutants are caught. The probes slipped against round three:
the final rubric is stricter, so more "fully supported" verdicts sit on the edge and move between
samples; 6 of 38 answers changed a verdict on re-sampling with nothing else changed. That is the
judge's own noise, about 16% per answer on this version; it averages out in a rate over 346
answers but matters for any per-answer use. The next judge version should set the judge model's
temperature to 0 (the config leaves it at the API default) together with the quota-row example;
both change the judge version and both are validated on new labels, not these.

## 4. Decision

- Hand-label agreement: **supported missed the bar (kappa 0.26 on judge-test)**; the cause is a
  rubric that the judge applies literally and the labels apply leniently and unevenly. Next
  step is the two-question rubric above, not a prompt tweak. "Responsive" is untested.
- Mutation tests and bias probes: mutants **passed** in every round; stability probes passed in
  round three (0.95) and slipped to 0.84 against the final, stricter judge (round four). Reported
  as such; temperature 0 is the planned fix for the next version.
- Agreement with hand labels: **pending** Aleena's labels (step 2). The judge does not ship, and
  no judge number appears in a headline, until kappa is reported on judge-test.
- What the judge is already allowed to do: flag dev answers where code says fail and the judge
  says responsive, as suspected grader misses for human confirmation (failure taxonomy, A).

## 5. Candidate judge v3 (2026-10-16, unvalidated)

Rubric v3 adds the quota-row example and the core/peripheral line for billing details that the
judge-test diagnosis pointed at. Temperature 0 was planned too, but the installed SDK
(anthropic 1.13) does not accept a temperature argument, so v3 is a rubric change only. It runs
under grader name `judge-v3` beside the validated judge, and was run on the current champion's
dev run (`docs-20261016-214710-02abb7`, 349 answers, 697 calls).

| Verdict | validated judge pass rate | judge-v3 pass rate | agreement |
|---|---|---|---|
| core supported | 0.943 | 0.925 | 95.4% |
| fully supported | 0.307 | 0.305 | 96.3% |
| responsive | 0.991 | 0.991 | 100% |

v3 is slightly stricter on core, as the quota-row example intends. Whether it is *better* is
decided by fresh labels: 40 answers of this run, labeled on the fixed screen, scored against both
judges with `evals judge-validate` (default config for the validated judge, `-c
configs/judge-v3.yaml` for the candidate). Those labels were never used to write rubric v3, so the
comparison is clean. Until then the validated judge stays in the gate.

## 6. Judge v2 or v3: decision rule, committed before scoring (2026-10-16)

Aleena labeled 40 attempt-1 answers of the champion dev run (`docs-20261016-214710-02abb7`) on
the fixed screen (citations first, rubric v2 text shown, no judge in view): 25 on the judge-test
side, 15 on the judge-dev side; core supported 37 yes / 3 no, fully supported 18 / 22,
responsive 40 / 0. Both judges (`2cf75fa03e09` validated, `9e7b588e83f8` candidate) graded these
answers before the labels existed. This rule was committed before either judge was scored against
them:

1. **Primary verdict: core supported** (the advisory one, and the reason v3 exists). v3 replaces
   v2 only if, on the 40, v3 agrees with the labels on more answers than v2 and the difference is
   beyond chance: exact McNemar p < 0.05 on the answers where exactly one judge agrees with the
   label.
2. **Guard: fully supported.** v3 must not agree on fewer answers than v2 by more than one, and
   McNemar on fully must not favour v2 at p < 0.05.
3. Otherwise **v2 stays** the validated judge, because every recorded number (the iteration 1 and
   2 gates, the changelog, the README) was produced by v2, and v2's fully-supported validation
   (kappa 1.00 on 65 blind labels) does not transfer to a changed prompt.
4. If v3 passes 1 and 2, its resample stability is measured on the same run before adoption,
   and every run in the changelog is re-judged with v3 so the history stays on one judge version.
5. Either way, both judges' agreement on the 40 is reported with intervals, split by label side
   and by whether the case's original-run disagreement informed rubric v3 (`docs-ai-004-p3`,
   `docs-ai-007`, `docs-ai-023` are among the 40), since those cases are not independent of v3's
   design; and the judge-dev side, where the labeler calibrated against v2's reasoning on the same
   questions in the earlier run, is reported separately.

Known before scoring: with 3 of 40 labels failing core, the core comparison can only resolve a
large difference, so rule 1 is unlikely to be met unless v3 corrects several answers v2 gets
wrong. If v3 looks better without meeting the rule, the number of further labels needed to decide
is computed from the observed discordance and reported.

## 7. Judge v2 vs v3 on 40 fresh labels: result and blind adjudication (2026-10-16)

Scored once, after the rule in section 6 was committed (`38c786f`), with
`scripts/judge_compare.py`; numbers in `results/judge-v2-vs-v3/scores.json`.

| Verdict | n | labeler fails | v2 agree | v2 kappa (95% CI) | v3 agree | v3 kappa (95% CI) | only v2 / only v3 agrees | McNemar p |
|---|---|---|---|---|---|---|---|---|
| core supported | 40 | 3 | 39 | 0.79 (0.00 to 1.00) | 38 | 0.64 (-0.03 to 1.00) | 1 / 0 | 1.0 |
| fully supported | 40 | 22 | 36 | 0.80 (0.58 to 0.95) | 36 | 0.80 (0.58 to 0.95) | 0 / 0 | 1.0 |
| responsive | 40 | 0 | 40 | undefined (no failures) | 40 | undefined | 0 / 0 | 1.0 |

The two judges gave identical fully-supported and responsive verdicts on all 40 and differ on
core on one answer.

**Decision under the rule: v2 stays the validated judge; v3 is not adopted.** Rule 1 fails (v3
agrees on fewer core answers, not more); rule 2 holds trivially. `configs/judge-v3.yaml` stays as
the record of a rejected candidate.

Pre-declared subsets:

| Subset | n | core: labeler fails / v2 agree / v3 agree | fully: labeler fails / v2 agree / v3 agree |
|---|---|---|---|
| judge-test side, not v3-informed | 22 | 1 / 22 / 22 | 10 / 20 / 20 |
| judge-test side, v3-informed | 3 | 1 / 2 / 1 | 3 / 2 / 2 |
| judge-dev side | 15 | 1 / 15 / 15 | 9 / 14 / 14 |

What the 40 add to v2's validation:

- **Fully supported** on fresh answers from the shipped v3 agent prompt: kappa 0.80 (0.58 to
  0.95), TPR 0.95, TNR 0.83. Above the 0.7 bar on the point estimate, below the 1.00 of the 65
  blind judge-test labels, and the interval reaches below the bar.
- **Core supported:** kappa 0.79 on these 40, but with only 3 labeler fails the interval spans
  0.00 to 1.00, so it neither confirms nor overturns the 0.37 on judge-test. Core stays advisory.
- **Responsive:** still untested on the agent's own answers; none of the 40 failed it.

### Blind adjudication of the 5 disputed answers

Workflow `adjudicate-judge-disagreements`: five independent reviewers per disputed answer, shown
only the rubrics, the question, the answer and the cited quotes, each applying rubric v2 and
rubric v3 separately; then, for each rater the panel's majority overruled, a defender who argues
that rater's verdict from the rubric's own words and says whether the case holds. Diagnostic
only: the hand labels remain the reference, and nothing here changes a label or the scores above.

**The first run was not blind, and was re-run.** Workflow subagents receive the user's global
CLAUDE.md, whose rule to start every task-oriented session with the task-observer skill led 11 of
the 25 first-run reviewers to load that skill; 9 located or searched the shared observation log
and 6 read it in full. That log describes this evaluation, including (observation 14) which way the
labeler read billing details. On docs-ai-023 all five reviewers had loaded the skill, and one of
them wrote observation 15 about the problem. The re-run used read-only reviewers that receive no
CLAUDE.md (`agentType: 'Explore'`, same model) and an audit of every transcript: 34 of 34 agents
made no tool call other than reading their named files, 77 calls in all, exactly the number the
task needs. Both runs' outputs, the hardened script and the audit are in
`results/judge-v2-vs-v3/`; only the clean run is reported below.

| Answer | Verdict | Clean panel, rubric v2 / v3 | Labeler | Judge v2 | Judge v3 | Reading |
|---|---|---|---|---|---|---|
| docs-ai-007 #1 | core | fail 5-0 / fail 5-0 | fail | pass | pass | both judges wrong: they match headerless table cells to Geographic and Global by elimination across rows; all four judge defenses failed |
| docs-ai-007 #1 | fully | fail 5-0 / fail 5-0 | fail | pass | pass | as above |
| docs-ai-023 #1 | core | fail 4-1 / fail 5-0 | pass | pass | fail | genuinely ambiguous: all five reviewers flagged v2's core definition for "how is X billed?"; the blind majority reads the price factors as core, and the defenses of both the labeler's and judge v2's reading held |
| docs-chg-003 #1 | fully | fail 5-0 / fail 5-0 | pass | fail | fail | label likely a slip: "stops providing patches" hardens the quote's "may no longer apply"; the defense of the label failed |
| docs-chg-004 #1 | fully | fail 5-0 / fail 5-0 | pass | fail | fail | label likely a slip under the plain reading: "The docs don't state the starting number" is a statement about what the documentation says; the defense failed |
| docs-chg-008 #1 | fully | fail 5-0 / fail 5-0 | pass | fail | fail | as chg-004, on "the linked 'Clarify availability change' page" and "The docs do not say explicitly how 'new customer' is defined"; the defense failed |

Against the contaminated first run, one majority changed: docs-ai-023 core under rubric v2, from
3-2 pass to 1-4 fail, the answer on which every first-run reviewer had loaded the skill and the
one the log described. That is consistent with the log having steered those reviewers, though
five votes cannot prove it. Every other verdict and every defense outcome repeated.

Findings:

1. **Rubric v3 did not do what it was for.** Its quota-row paragraph changed neither judge's
   verdict on docs-ai-007, one of the cases it was written for. Its billing paragraph takes the
   reading that blind reviewers also take from v2's own definition, but it is the opposite of the
   labeler's, and the labeler is the reference: it cost v3 the one answer it lost.
2. **A judge error neither rubric fixes:** matching cells to columns by elimination across
   headerless rows. A future judge version could name it explicitly; it would need fresh labels,
   because these 40 are now its development data.
3. **Two rubric questions for the labeler**, each settling a disagreement that belongs to the
   rubric, not to anyone's mistake:
   - Is "the docs don't say X" a hedge, which needs no support, or a statement about what the
     documentation says, which the rubric counts as a claim? (docs-chg-004, docs-chg-008)
   - When a question asks how something is billed, are the price factors part of the main
     answer? (docs-ai-023)
4. **Three fully-supported labels** (docs-chg-003, docs-chg-004, docs-chg-008) are offered back to
   the labeler for a second look on the label screen, without the judge in view. Any change is
   recorded as a correction made after adjudication, and the scores above stay as first computed.
5. **Isolation must be checked, not assumed.** A subagent told to read one named file still
   followed a standing user rule to load a skill and read a shared log. Blind or isolated agents
   now run as an agent type without CLAUDE.md, and their transcripts are audited; the expected
   tool-call count is a cheap leak detector.

### Decisions on the two rubric questions (Aleena, 2026-10-16)

1. **"The docs don't say X" is a hedge** and needs no support. Written into draft rubric v4
   (`SUPPORT_RUBRICS["v4"]` in `graders/judge_prompts.py`, `configs/judge-v4.yaml`).
2. **Price factors are not part of the main answer** when a question asks how something is
   billed; they are peripheral. Wording drafted by the agent and approved by Aleena on
   2026-10-17; it is in rubric v4.

What follows from them:

- **Labels.** For docs-chg-004 the clean panel's only unsupported statement was an absence
  statement ("The docs don't state the starting number"), so under decision 1 the label (fully
  supported: yes) stands. docs-chg-008 turned on an absence statement plus a page name inside the
  same closing hedge; the label already reads the whole caveat as a hedge, and stands on that
  reading. docs-ai-023's label (core: yes) matches decision 2. docs-chg-003 ("stops providing
  patches" for the quote's "may no longer apply") is untouched by either decision and is the one
  label still offered back for a second look.
- **Judge.** v2 stays the validated judge. It counts absence statements as unsupported claims, so
  its fully-supported verdict is stricter than the labeler's rubric on that point; this is now a
  documented difference, not a disagreement to re-score. Judge v4 (draft) waits for the billing
  wording; then it is run on a fresh run's answers and scored against fresh labels, as in
  section 6.
- **Headline metric.** Decision 1 relaxes "fully supported", so the iteration-1 numbers (10.6% to
  31.8% on test) are v2-judge numbers and stay labelled as such until a re-judge under an adopted
  v4.

Recheck result (2026-10-17): Aleena re-opened docs-chg-003 on the label screen, without the judge
in view, and kept "fully supported: yes". The label stands as the reference; the panel's and both
judges' reading ("stops providing patches" is stronger than the quote's "may no longer apply")
is recorded as a disagreement with it, not a correction.

## 8. Judge v4 (draft): decision rule, committed before any v4 scoring (2026-10-17)

Judge v4 is rubric v2 plus the labeler's two decisions of 2026-10-16: a statement that the
documentation does not say something is a hedge, and price factors are peripheral for billing
questions (wording final 2026-10-17; judge version `bf1a8e9f376a`). Unlike v3, v4 is not a tuning attempt: it changes the definition the
labeler applies, so v2 is now known to diverge from the reference on a named pattern. The test is
whether v4 implements the revised definition at least as well as v2 implemented the old one.

**Labels.** The next 40 unlabeled attempt-1 answers of the champion dev run
(`docs-20261016-214710-02abb7`, in case-id order, so none of the 40 used in section 7), labeled with
`-c configs/judge-v4.yaml` so the label screen shows rubric v4 and no judge. Judge v2 has already
graded these answers; judge v4 grades them once its billing wording is final, before scoring.

**Rule.**

1. **Fully supported** (the headline, validated verdict): v4's kappa on the 40 is at least 0.7, v4
   agrees with the labels on at least as many answers as v2, and exact McNemar on the answers where
   exactly one judge agrees with the label does not favour v2 at p < 0.05.
2. **Core supported:** v4 agrees with the labels on no fewer answers than v2 minus one.
3. **Probes:** on the champion run, v4's number, qualifier and citation mutants are each caught at
   least 90% of the time, with v2's probes run on the same run for comparison; resample
   stability is reported for both, not gated (v2 measured 0.84 on the original dev run).
4. If 1 to 3 hold, **v4 replaces v2**: every run reported in the changelog and README is re-judged
   with v4, and their numbers are restated under v4 with the v2 numbers kept beside them. If any
   fails, v2 stays and the miss is recorded here.

### Result (2026-10-17): v4 fails rule 3 by one probe, so v2 stays

Scored once with `scripts/judge_compare_v4.py`; the output is in `results/judge-v2-vs-v4/`. The
40 labels are docs-chg-010 to docs-int-037, made 03:13 to 03:55 UTC with rubric v4 on the label
screen, and exported (`7bb575c`) before judge v4 graded anything. A judge-free audit before
scoring found every "no" noted, "core no" always paired with "fully no", and no note written for
another answer. Judge v4 graded all 349 answers of the run as `bf1a8e9f376a` (its "responsive"
prompt is v2's, so those verdicts came from the call cache and are identical).

| Check | v2 | v4 | Bar | Result |
|---|---|---|---|---|
| 1. fully supported: agreement, kappa (95% CI) | 38/40, 0.86 (0.61 to 1.00) | 38/40, 0.87 (0.64 to 1.00) | v4 kappa at least 0.7, agreement at least v2's, McNemar not for v2 | pass |
| 2. core supported: agreement | 38/40 | 39/40 | at least v2 minus one | pass |
| 3. number mutants caught | 8/8 | 9/9 | at least 90% | pass |
| 3. qualifier mutants caught | 7/8 | 8/9 (89%) | at least 90% | **fail** |
| 3. citation mutants caught | 40/40 | 40/40 | at least 90% | pass |
| resample stability (reported, not gated) | 0.95 | 0.97 | | |
| padding (reported, not gated) | 0.97 | 0.95 | | |

Under the rule, **v2 stays the validated judge** and nothing is re-judged. The miss is recorded
here.

On fully supported the two judges err on different answers: v2 has one false fail and one false
pass, v4 two false passes and no false fail (exactly one judge right on two answers, one each way;
McNemar p = 1). Over all 349 answers of the run they agree on 94.3% of fully-supported verdicts:
v4 passes 13 answers that v2 fails and v2 passes 7 that v4 fails. That would move the run's
headline from 0.308 to 0.326 (per case, n = 118), inside either interval. Detecting v4's one-answer
gain on core supported at 80% power would take about 314 labels.

**What the miss was** (diagnosed after scoring, so it does not change the result). The one
qualifier mutant v4 passed is the one v2 passed too: docs-ai-003-p1 attempt 1, where the
generator turned "Note that this is about opt-in only" into "this is exactly opt-in only".
"About" there means "concerning", so the mutant plants no false fact, and passing it was
defensible. The generator now flips "about" and "approximately" only before a figure (probe
round five, with a test). Without that mutant both judges catch every planted error; that is a
post-hoc observation and is not used to overturn the rule.

**Disputed answers.** Seven answers where the labeler and either judge differ on any verdict are
packaged in `results/judge-v2-vs-v4/disputed-answers.json`: two on responsive (both judges fail
answers the labeler passed; the two judges' responsive verdicts are identical), two on core and
three on fully supported. They were not adjudicated, since no outcome could change the decision.

**Reporting fix found during this round.** Run summaries, `evals compare` and `evals gate` read
grades by metric name, so on a run graded by more than one judge they averaged the judges'
verdicts as if they were extra attempts. Only the champion run carries more than one judge (v2,
v3 and v4). Recomputed with the fix, every published number is unchanged: both gates that use
this run were saved before the second judge graded it. They now read only the configured judge
(`-c` picks another), and the report names which one it shows.

## 9. Judge v2.1: rubric v2 plus one paragraph on reporting phrases (rule committed 2026-10-17, before any v2.1 grading or scoring)

**Why.** Iteration 4's pre-registered spot check (`docs/plans/iteration-4.md`) found the
validated judge passing 3 of 30 v5 answers that Aleena failed. Two were attributions the judge
credited from the cited URL: "according to the Amazon Bedrock cross-Region inference
documentation", and "this is stated in the Aurora Serverless v2 user guide, so this applies to
Aurora Serverless v2". Her earlier labels pass plain reporting frames ("The documentation says
...", "According to the Amazon Bedrock docs ..."). Rubric v2.1 adds one paragraph that states
the line, in `src/agent_evals/graders/judge_prompts.py`, with judge version `88db72ba4022` and
config `configs/judge-v2.1.yaml`.

**Review before commit.** Two isolated reviewer agents checked a draft against all 224 labeled
answers. They found 101 with an attribution, and the draft matched her verdicts on 95. The final
wording fixes the six disagreements. The publisher, the service's own name and generic words for a
kind of page add nothing. The question can clear only the source a phrase names. Pointers to
other pages are reported content. The test applies only to names a reporting phrase introduces.
Spot checks of the final wording: docs-ai-045, docs-ai-032, docs-core-039, docs-ai-004, docs-ai-011
and docs-ai-028 come out supported, as she labeled them; docs-ai-001-p2, docs-mix-049 and
docs-core-028 come out unsupported, as she labeled them. Known residual risk: a question that
names the wrong version clears "according to the v1 docs" for the source name.

**Labels.** The 30 fresh labels are the next 30 answered attempt-1 answers of
`docs-20261017-174739-1c0e8e` in the spot check's hash order, positions 31 to 60: docs-ai-005-p1, docs-ai-002, docs-int-020, docs-mix-055, docs-chg-023, docs-ai-005, docs-ai-005-p2, docs-int-041, docs-ai-033, docs-mix-056, docs-core-011, docs-ai-029, docs-chg-013, docs-int-029, docs-ai-002-p2, docs-int-007, docs-core-034, docs-mix-050, docs-core-018, docs-core-025, docs-ai-038, docs-core-039, docs-mix-060, docs-mix-085, docs-chg-010, docs-ai-001, docs-chg-019, docs-ai-007, docs-core-016, docs-mix-077.
They are labeled with rubric v2 on screen and no judge in view, and none was used to write the
paragraph.

**Agreement** is counted per verdict, fully supported and core supported separately, over
labeled attempts that have citations. Ties count as "at least as many". A false pass is a judge
pass on an answer the labeler failed.

**Rule.**

1. **Fresh labels.** On the 30 fresh labels, v2.1's agreement is at least v2's on fully supported
   and on core supported. Its false passes on fully supported are no more than v2's and at most 2.
   If v2 and v2.1 give identical fully-supported verdicts on all 30, the 30 cannot tell them apart.
   Aleena then labels positions 61 to 90 under the same conditions, and this criterion is applied
   to all 60.
2. **Earlier labels.** The earlier sets are 114 labels on `docs-20261011-223111-702e87`, 80 on
   `docs-20261016-214710-02abb7` and the 30 spot-check labels on `docs-20261017-174739-1c0e8e`.
   v2.1 is graded once with the default salt and compared with the stored v2 verdicts. In each
   set, v2.1's agreement on each support verdict is not lower than v2's by more than 1. Summed
   over the three sets, it is not lower than v2's on either verdict. The spot-check 30 produced
   the paragraph, so v2.1's expected gain there is not evidence for adoption; they enter only as a
   non-inferiority check.
3. **Probes.** Probes run on `docs-20261017-174739-1c0e8e`, built from the answers both judges
   pass on both verdicts (the first 40 in case-id order), and both judges grade the same variants.
   For each mutant kind (number, qualifier, citation), v2.1 catches at least as many as v2 minus
   one. Two things are reported but not gated: resample stability, and a harmless-attribution
   probe that prefixes each passed answer with "According to the documentation, ". Its verdicts
   should not change.
4. **If 1 to 3 hold:** v2.1 becomes the validated judge. `default.yaml` gets rubric_version v2.1
   and grader_name judge-v2.1. Every reported run is re-judged with v2.1, and its numbers are
   restated with v2's beside them. The iteration-4 gate is re-read under v2.1, and the fresh 30
   count as v2.1's spot check on v5's answers, under the same bar of at most 2 false passes. If the
   gate's outcome flips under v2.1, both readings are reported and only the new one is acted on.
5. **If any criterion fails, even narrowly:** v2 stays, iteration 4's number stays untrusted, and
   the miss is recorded here. A revised paragraph may be tested only on labels collected after it
   is written, positions 61 onward.

### Result (2026-10-17): v2.1 fails criterion 1 on core supported, so v2 stays

Scored once with `scripts/judge_compare_v2_1.py`; outputs are in `results/judge-v2-vs-v2.1/`. The
fresh labels were exported (`34f28f1`) before any v2.1 grade existed. v2.1 was graded once with
the default salt on the v5 run and on the labeled answers of the two earlier runs.

| Criterion | v2 | v2.1 | Bar | Result |
|---|---|---|---|---|
| 1. Fresh 30, fully supported: agreement | 26 | 28 | at least v2 | pass |
| 1. Fresh 30, fully supported: false passes | 4 | 2 | at most v2's, and at most 2 | pass |
| 1. Fresh 30, core supported: agreement | 29 | 27 | at least v2 | **fail** |
| 2. Earlier sets, per set, fully / core | 114, 74, 27 / 103, 77, 30 | 113, 73, 30 / 105, 77, 30 | not lower by more than 1 | pass |
| 2. Earlier sets, summed, fully / core | 215 / 210 | 216 / 212 | at least v2 | pass |
| 3. Mutants caught: number, qualifier, citation | 11/11, 11/11, 40/40 | 11/11, 11/11, 40/40 | at least v2 minus one | pass |
| Reported only: padding, resample, harmless attribution | 40/40, 40/40, 39/40 | 39/40, 40/40, 40/40 | | |

Under the rule, **v2 stays the validated judge**, and iteration 4's v5 number stays untrusted.

What happened on the fresh 30. v2.1 did what the paragraph was for: false passes on fully
supported fell from 4 to 2, and agreement rose from 26 to 28. The two core failures, docs-int-020
and docs-int-029, are not attributions. In both, v2.1 flagged the same unsupported claim that
Aleena's note names, so its fully-supported verdict agrees with hers. But it called that claim
core where she called it peripheral:

- docs-int-020: the premise correction "Parameter Store does not bill per parameter like Secrets
  Manager".
- docs-int-029: the answer drops the quote's "most" from "most managed secrets".

Over the 60 labeled v5 answers, fully-supported agreement is v2 53/60 and v2.1 58/60. The second
figure includes the spot-check 30 that informed the paragraph.

Facts that bear on the next step (post hoc, not part of the rule):

- Aleena's labels on v5's answers, all 60 hash-sampled, give fully supported 35/60 (0.58). Her
  80 labels on v3's answers give 27/80 (0.34). The v3 labels are the first 80 in case-id order, not
  a random sample.
- v2's false passes on fully supported are 7 of 60 on v5's answers and 2 of 80 on v3's. The judge
  is lenient on the improved answers, as the spot check found.
- The README reports core supported as advisory, and the gate does not monitor it. Gating
  adoption on it was this rule's choice, made on a reviewer's advice. It is recorded as a lesson,
  not changed after the fact.

