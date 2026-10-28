# Proposal: the claim judge (judge v5), the judge improvement round

**Summary.** The project's LLM judge decides whether an answer is fully supported by the quotes
it cites. It validated well on dev answers but later passed too many unsupported answers, so it
was kept out of the online alert. This proposal replaces its single overall verdict with a claim
table that code can check, a confidence score per claim with one tuned threshold, and a skeptical
second pass. Validation is fixed in advance: tuning on dev labels, a regression screen on older
labels, then one test on fresh blind labels, the only stage that can admit the new judge to the
alert. Status: proposed, reviewed in three adversarial rounds, not yet built.

**Status:** proposed 2026-10-28 and revised the same day after three rounds of adversarial
review (34, 27 and 9 findings, all taken in; the tables at the end). Not yet approved. It changes
`graders/`, so under AGENTS.md the approach is agreed before any code is written, and the paid
steps are approved separately. The direction was agreed in principle on 2026-10-21 (phase 7 plan,
"Agreed in principle 2026-10-21"): claim-by-claim checking, a 0 to 100 score with a tuned
threshold, and a skeptic pass. This document turns that into a design, a validation plan whose
rules are fixed now, a cost, and seven open decisions (the last section).

"Judge v5" is the judge's own version number (after v2, the validated one, and the v2.1, v3 and
v4 candidates). It has nothing to do with the docs agent's prompt v5 of iteration 4, so this
document calls the new judge "the claim judge".

## In short

- **Why.** The validated judge (v2) passes too many answers that are not fully supported. On the
  online check it passed 15 reference-day answers, and the author's blind labels failed 4 of them,
  against a bar of 2, so `judge_supported` cannot alert. Four repair rounds on three rubric
  changes never got a replacement adopted.
- **What changes.** The judge stops giving one overall verdict. It lists the answer's claims and,
  for each, quotes the words that support it and says how sure it is, from 0 to 100. Code checks
  that every quoted span is really in the answer and in the quote, and that every number and
  name in the answer is covered by some claim, and decides the verdict: fully supported only if
  every claim clears one threshold. A second, skeptical call re-scores what code flagged and
  looks for a missed claim. The judge no longer sees citation URLs (a URL is not a quote, and it
  is how v2 credited attributions), and its reply is longer (`max_tokens` 8,192).
- **What stays.** The model (claude-sonnet-5-5), the question, answer and quotes as input, never
  the gold answer, the skips, the "responsive" verdict, the 25% answer-length trust rule, and
  judge v2 as the offline judge of the improvement loop.
- **The honest limit of the old labels.** On the existing labels, all of v2's false passes, and
  all of its errors on the tuning set, sit on cases the design was built from: outside them it
  makes no false pass at all. So the old labels can catch a new judge breaking, but cannot show
  it is better. Only fresh blind labels, drawn after the judge is frozen, can, and only they can
  put `judge_supported` into the alert family.
- **Human time.** About 15 minutes for the seven decisions, then 60 or 30 blind labels (about 55
  or 30 minutes), only if every earlier stage passes.
- **Money.** About $45 to $60 before any label, measured on a pilot first (or a leaner run at
  about $30); about $17 to $22 more only if the judge passes; and online judging would then cost
  about $3 instead of $1 a day.

## Why the current judge falls short

Judge v2 reads the question, the answer and the cited quotes, each shown with its URL, and
returns two verdicts and one sentence (`src/agent_evals/graders/judge.py`, `supported_prompt` in
`graders/judge_prompts.py`). On dev answers it validated well: all 65 blind judge-test labels
agreed (kappa 1.00), then 36 of 40 and 38 of 40 fresh labels (`docs/judge-validation.md`,
sections 2f, 7 and 8). Four later checks on other answers found false passes:

| Check | Answers | Judge v2's false passes | Bar | Source |
|---|---|---|---|---|
| Iteration 4 spot check (prompt v5's answers) | 30 | 3 | 2 | `docs/plans/iteration-4.md` |
| Rubric v2.1 test, fresh 30 (v2 scored beside it) | 30 | 4 | (v2.1's bar 2) | `docs/judge-validation.md` section 9 |
| Rubric v2.1 retest, the next 30 (v2 scored beside it) | 30 | 2 | (v2.1's bar 2) | `docs/judge-validation.md` section 10 |
| Online judge check (reference days 1 to 5) | 15 it passed | 4 | 2 | phase 7 plan, part 3 |

What the misses have in common, from the label notes and the adjudications:

1. **Attributions credited from the URL.** "According to the Amazon Bedrock cross-Region
   inference documentation" passed because the cited URL said cross-Region inference, though no
   quote did (section 9).
2. **One flaw among many claims.** In the old answers an unsupported phrase usually sat beside
   others, so the answer failed anyway; in the improved answers it is often the only flaw, and a
   single overall verdict lets it through (iteration 4). Over the 90 labels of prompt v5's
   answers v2 made 9 false passes; over 80 of v3's, 2.
3. **Repairs traded one error for another.** Rubric v2.1 cut false passes from 4 to 2 on its fresh
   30 (section 9), but on its retest it also failed hedges about the documentation ("the docs
   don't state the reduced number"), which the labels treat as no claim at all (section 10). Of
   the four rounds, on rubrics v3, v4 and v2.1 (tested, then retested), three missed narrowly: v4
   by one planted-error probe, v2.1 by two answers on core supported, and its retest by one
   answer. v3 did not come close (sections 6 to 10).

The online check's own errors show the same patterns (from the database and the label notes):

| Answer | Judge v2 / label | The claim at issue | Pattern |
|---|---|---|---|
| online-d01-085 | pass / fail | "I confirmed this from the AWS Batch overview page" (v2: "not factual claims") | a statement about a named source |
| online-d03-068 | pass / fail | "based on the SNS dead-letter queue guide's alarm example" | a statement about a named source |
| online-d02-071 | pass / fail | "Reserved Instances", named only in the page text around a quote | a term no quote states |
| online-d04-030 | pass / fail | "Only MRSC gives latest-version reads" | a strengthened scope ("only") |
| online-d04-032 | fail / pass | "Check KNNGraphMemoryUsage ... against knn.memory.circuit_breaker.limit" | advice that names a parameter |
| online-d04-072 | fail / pass | "Scripting can help with checking MFA status" | advice |

So the problem is less the rubric's words than the shape of the task: one holistic verdict per
answer, made with the URLs in view, with no record of which claims were checked. The two false
fails also show where the rubric is unclear: it calls advice no claim, but counts "names of APIs
or parameters" as peripheral claims, and the labels pass advice that names them.

One more measure matters for the alert. A fault moves the judge's "not fully supported" line by
the true change times J, where J is the share of unsupported answers the judge fails plus the
share of supported answers it passes, minus 1. Projecting the first check's 30 labels onto the
376 reference-day answers left fresh, pooled over days, v2's J is about 0.55 (about 0.57 on the
406 answers they were drawn from, 0.54 weighted by day): it fails about 91% of unsupported
answers but passes only about 65% of supported ones, so a true rise of about 9 points moves its
line by the 5-point floor. These estimates rest on 15 labels per verdict and are wide.

## The design

### What stays the same

- The model, claude-sonnet-5-5, through `LLMClient`, so every call is cached, timed and counted.
- The input: the question, the answer and the cited quotes; never the gold answer.
- The skips: errored, abstained and uncited answers get no support verdict.
- `judge_responsive` is untouched and not part of this round. The claim judge's config makes no
  responsive call, so it adds no paid calls on dev runs.
- `judge_supported` and `judge_core_supported` stay 0 or 1 per answer, so every report, gate and
  monitor rule reads them as before.
- The monitor's 25% answer-length trust rule applies to the claim judge unchanged.
- Judge v2 stays the validated offline judge, and the online judge unless stage C passes. The
  claim judge runs under its own grader name, `judge-v5`, beside it, and never under "judge".

### Part 1: a claim table that code can check

One call returns a table through a strict tool, `submit_claims`:

```json
{
  "claims": [
    {
      "answer_span": "exact words from the answer that make the claim",
      "role": "core | peripheral",
      "support": [{"quote": 2, "quote_span": "exact words from quote 2"}],
      "score": 0,
      "why": "one short sentence"
    }
  ],
  "not_claims": ["exact words from the answer that are advice, hedges or offers"]
}
```

- **The quotes are numbered and shown without their URLs.** A URL is not a quote (rubric v2.1
  already says so), and the URL is how attributions were credited.
- **Spans are checked in code.** Text is normalized first: whitespace, quote marks, backticks,
  table pipes and dashes. Each `answer_span` must occur in the answer, and each `quote_span` in
  the quote it names and be at least three words long. A support entry that fails the check is
  dropped; a claim left with no valid support entry is unsupported. Dropped entries are counted,
  and so are the verdicts that flip only because of one; both are reported at every stage.
- **Coverage is checked in code.** Every number in the answer, and every capitalized word that
  does not start a sentence and is not in the question, must fall inside some claim's
  `answer_span` or a `not_claims` span. An uncovered one, or one inside a `not_claims` span, is a
  flagged item for the skeptic (part 3).
- **Numbers are cross-checked.** A number in a claim's `answer_span` that appears in none of its
  `quote_span`s makes that claim a flagged item too (it may be a correct unit conversion, which
  the rubric allows, so it is re-scored, not failed).
- Code checks that spans exist, not that they support the claim. Support is still the judge's
  call; the scores and the skeptic are what test it.

### Part 2: a confidence score per claim, and one tuned threshold

Each claim's `score` is the judge's confidence, from 0 to 100, that the cited quotes support the
claim under the rubric: "supported" keeps the rubric's own meaning (a quote states it, or it
follows by simple arithmetic, a unit conversion or plain rewording), and the score says how sure
the judge is of that. A claim counts as supported when its score is at least the threshold *t*
and it keeps at least one valid support entry. The answer is **fully supported** when every
claim is supported, and **core supported** when every core claim is.

*t* is a calibration choice, not a change of rubric: a higher *t* passes only claims the judge is
surer of. It is chosen once in stage A from {50, 70, 85} and is part of the judge's version, so
any other *t* is a new judge that needs new labels. The lowest claim score is stored as a
reported-only metric, `judge_support_score`, for audit; it is never used to re-read verdicts at
another threshold, and it stays out of every gate, rate and alert.

### Part 3: a skeptic pass

The judge's errors are mostly false passes, so a second call checks what the table may have got
wrong. It runs on every answer that passes at the lowest *t* in the grid (50), and on every
answer with a flagged item. Its request never contains *t* or *s*, so the same request, and the
same cached reply, serves every cell of the grid and production alike. It gets the question, the
answer, the numbered quotes, the claim table and the flagged items, and returns two things:

- **For each flagged item, a support score** on the same 0 to 100 scale as the table, keyed by
  the item's number (code, not the skeptic, says which items exist). A flagged item counts as
  supported only at or above *t*, the same bar as every claim, or as not a claim if the skeptic
  says so and code finds no number in it.
- **A general search:** the one claim most likely unsupported, including any the table missed, as
  an exact `answer_span`, its role (core or peripheral), why no quote states it, and its
  confidence from 0 to 100; or "none found". It is pointed at the patterns behind the past misses
  (described generally, without quoting any case): statements about a named source; terms no
  quote states (features, versions, editions, Regions, pricing models); strengthened scope or
  qualifiers ("only", "always", "most", "up to", "by default"); numbers and units; and table
  cells read as a default or a maximum when no quoted header says so. A verified span at or above
  the threshold *s* fails fully supported, and core supported too when its role is core. *s* is
  chosen in stage A from {off, 70, 90}.

Both prompts name hedges, statements that the documentation does not say something, and advice
as no claim at all, the line rubric v2.1 crossed (under decision 2(b) this changes; see there).
A general-search span that is not in the answer is retried once under a second salt; if it is
still not there, the search counts as "none found" and the event is counted.

### Judge errors, so that a day can always be read

The monitor refuses a day if any cited answer lacks a grade, and a failed reply would replay from
the cache. So any reply, from the table call or the skeptic call, that fails validation (a
missing or malformed tool call, a truncated reply, an `answer_span` not in the answer, missing or
unknown item keys in the skeptic's scores) is retried once under a second salt recorded in the
run note, and the retry's reply replaces the first in full. If the retry also fails, the answer
gets a `judge_error` grade instead of a verdict:

- it is not a verdict, so it is left out of `judge_supported`'s values everywhere, and counted;
- `load_day` accepts it in place of a verdict, and the monitor marks the judge untrusted on a day
  where judge errors pass 2% of the cited answers, as it does for a 25% style shift;
- it counts toward every judge-error cap below;
- the stage C draw leaves judge-error answers out, so a sampled answer always has a verdict, as
  phase 7's check requires ("a sampled answer without a judge verdict is refused, not read as a
  judge fail").

### What the grader records

- Grades `judge_supported` and `judge_core_supported` (0 or 1), `judge_support_score`, and
  `judge_error` when it applies.
- The reason: the first unsupported claim's span and why (from the table or the skeptic), or
  "every claim is quoted", plus any flagged item.
- The full claim table and skeptic reply stay in the call cache, as every call does.
- The judge version is a hash of the model settings (including `max_tokens` 8,192), both system
  prompts, both message templates, both tool schemas, *t*, *s*, the normalization and coverage
  rules, and a version number for the grading code. (Today's hash leaves out the message
  template and the grading logic; the claim judge's input format, quotes without URLs, is exactly
  the kind of change that must move it.)
- The model thinks before its tool call in about three quarters of today's judge replies, and
  temperature cannot be set with the installed SDK, so stability is measured, not forced.

### Why this is more versatile

- **Length and style.** The verdict is a rule over claims, and every fact must be accounted for by
  the coverage check. Whether that makes it steadier across answer lengths is measured, not
  assumed: stage B gates a probe that adds supported claims to passing answers. The 25% trust
  rule still guards the drift the probes cannot reach.
- **Auditable.** Every fail names a span; every pass rests on spans that exist where the judge
  says they do.
- **Reusable.** Nothing in it is specific to AWS: any agent that answers with quoted citations
  can be judged this way, the code agent's explanations included.

## The rubric (decision 2)

The table and the skeptic apply a rubric, and the hand labels are the truth it is measured
against, so the judge and the label screen must show the same text (`judge.rubric_version`,
under a new key). Proposed: rubric v4 (rubric v2 plus the two rubric decisions of 2026-10-16: a
statement that the documentation does not say something is a hedge, and price factors are
peripheral in billing questions), plus two paragraphs:

> A phrase that reports the documentation ("the documentation says", "according to the Amazon
> Bedrock docs", "AWS recommends") is not a claim by itself; what it reports is. But a feature,
> version or edition name that such a phrase introduces, and that neither the question nor any
> quote uses, is a peripheral claim of its own: "according to the cross-Region inference
> documentation" is unsupported when no quote mentions cross-Region inference. A citation's URL
> and the page text around a quote are not quotes. This paragraph is about reporting phrases
> only: a statement that the documentation does not say something, or that a page could not be
> opened, is a hedge under the rule above.
>
> Advice stays advice when it names an API, a command, a parameter, a metric or a tool ("check
> your account's actual value in the Service Quotas console, or with aws lambda
> get-account-settings"); only a statement of fact about one of them is a claim.

Both examples are from dev-split answers (docs-ai-001-p2, and docs-chg-004 on the champion's dev
run), as AGENTS.md requires of prompt examples; the online check's cases are used in neither the
rubric nor the prompts. The labels made since 2026-10-16 already follow v4's two decisions (in
section 10's retest, "the docs don't state the reduced number" was labeled supported with rubric
v2 on screen), and the labels pass advice that names a command or parameter (docs-chg-004 above,
and two on the online check), which the last paragraph writes down. The first paragraph reuses
rubric v2.1's wording, which was shaped against 224 labels; that is one reason the older labels
cannot validate this judge (stage B).

The alternative is rubric v2 unchanged. It keeps the fresh labels under the same text as most past
labels (40 were made under v4), but rubric v2 has no hedge carve-out and counts names of APIs and
parameters as claims. Under it, the prompts would carry only rubric v2's rules, statements that
the documentation does not say something would count as claims (as v2 judges them), and the
hedge probe and the hedge regression rules below would be reported rather than gated, since
rubric v2 does not state the reading they test.

## Validation, with the rules fixed now

### The data, and what it can and cannot show

From the results database on 2026-10-28 (read-only): 375 hand labels, all by the author, all on
attempt 1, split by a hash of the case id into judge-dev (168) and judge-test (207). None is on a
test-split case, and no test-split run is used anywhere in this round.

- **Named cases.** Every case this document quotes, paraphrases or built a pattern from is listed
  in the appendix, frozen now. Cases a stage A revision later quotes stay in whichever counts they
  are in today, and are listed as added.
- **Tuning (stage A): the 125 judge-dev labels with citations on dev-split cases** (49, 33 and 43
  on the first dev run, the champion's and iteration 4's). AGENTS.md lets learning read only dev,
  so the online check's labels, whose cases are split "online", are not tuned on. Of the 125, 19
  are named cases, and they hold all of v2's errors there: 6 false passes and 3 false fails. On the
  other 106 (30 labeled supported, 76 not) v2 makes none. Some of those labels were revisited
  with v2's verdicts in view during its calibration (`docs/judge-validation.md` sections 2c to
  2e), so agreement with v2 there is partly by construction.
- **Regression screen (stage B): the 171 judge-test labels with citations** (65, 47 and 47 on the
  three dev runs, 12 on the online check). Of these, 29 are named cases, holding v2's 6 false
  passes and 3 false fails; on the other 142 (55 labeled supported) v2 makes 2 false fails and no
  false pass. These labels are not held out: all were scored against v2 before, and at least 113
  of them shaped this design (the 65 on the first dev run produced the quota-row reading behind
  the skeptic's table-cell pattern, and section 2f says the next judge "must then be validated on
  new labels, never on these 65"; the 47 on iteration 4's run produced and tested the wording the
  first rubric paragraph reuses).
- **So the existing labels can show breakage, not improvement.** Every false pass v2 makes on them
  is on a named case, so any false-pass gain before stage C comes only from the cases the design
  was built from. Stages A and B therefore gate on new errors, with one build check (stage A
  step 6), and report the named cases' results as built in.
- **Named answers labeled supported.** Where v2 also passes them, a new judge must too: 1 such
  answer in the tuning set and 4 in the screen. Where v2 fails them (3 in the tuning set, among
  them two headerless table rows the design reads strictly, and 3 in the screen, all hedges or
  absence statements), the results are reported as built in.
- **Fresh (stage C):** on the five reference days, 376 judged answers have never been labeled and
  were not shown on the dashboard. The fresh sample is drawn from these after the judge is frozen,
  so these are the only labels collected after the change is written (section 9's rule 5).

### Stage A: build and tune (no new labels)

1. **Pilot:** 20 answers from the tuning set, each under three salts, to measure the real cost
   per answer, the flip rate, and the retry, judge-error and dropped-span rates. If the cost per
   answer is more than 1.5 times the estimate below, the work stops and the budget is revisited.
2. **Each version is judged once on all 125.** A version is the first draft or one of at most
   three revisions, and any change to the prompts or the grading code counts as a revision. Each
   is made from the tuning labels only. The grid is *t* in {50, 70, 85} and *s* in {off, 70, 90},
   read from that one pass.
3. **Eligible cells:** on the 106 answers outside the named cases, errors at most v2's on the same
   answers plus 2, of which false fails at most v2's plus 1; and the named answer labeled
   supported that v2 also passes is passed.
4. **Candidates:** among eligible cells of all versions, the two best by fewest errors on the 19
   named cases (the build working as intended, not evidence), then fewer errors on the 106, then
   lower *t*, then *s* off before 90 before 70.
5. **Choice on three passes:** the two candidates grade the 125 under two more salts, and v2 does
   too, in `scripts/judge_compare_v5.py` without storing grades. The frozen version is the better
   candidate by step 4's order, on its three-pass means, that is still eligible on those means
   (an answer counts as passed when it passes on at least two of the three passes).
6. **Stops:** the round stops, with the miss recorded, if no cell of the first draft or of a
   revision is eligible after the last revision, if neither candidate is eligible on its means,
   or if the frozen version does not make fewer errors than v2 on the 19 named cases, on its
   means (a judge that does not fix the cases it was built from is not built right).

Every version's numbers are recorded here, including those dropped. Approval of decision 1
covers these up to three revisions in `graders/`; anything beyond them is a new proposal.

### Stage B: a regression screen (scored once, before any new label)

The frozen claim judge grades the 171 under three salts and v2 under two more, beside its stored
verdicts, in the script without storing grades and never under the name "judge" (re-judging
under it would change v2's check records). Counts are means over the three passes. All must
hold:

1. **No new strictness:** on the 142 outside the named cases, false fails at most v2's plus 2
   (v2's stored pass has 2).
2. **No hedge or advice regression:** the 4 named answers labeled supported that v2 also passes
   each pass on at least two of the three passes.
3. **Probes,** on the champion's dev run (`docs-20261016-214710-02abb7`), built with the current
   generator (round five) from every attempt-1 answer that v2 passes on all three verdicts and
   the claim judge passes on fully supported (at most 33), scored on `judge_supported` only, one
   pass each. Expected n: about 18 number mutants, at most 7 qualifier mutants, and one citation
   mutant per base. For each mutant kind, the claim judge catches at least as many as v2 minus
   one. Two new probes, each with verdicts allowed to change on at most 2 of the bases: a hedge
   probe (appending "The documentation does not say whether this differs by Region." to each
   base), and a supported-claims probe (appending one sentence copied word for word from the
   base's own quotes).
4. **Errors:** judge errors and verdicts flipped only by a dropped span, together, are at most 2%
   of the 171 in every pass.

Reported, not gated: false passes (outside the named cases, where v2 has none, and on them),
agreement, core supported (advisory, as for v2), the flip rate, padding, a harmless-attribution
probe ("According to the documentation, " prefixed to each base), and a swapped-service-name
mutant. A stage B failure ends the round, and is recorded.

### Stage C: the online check on fresh blind labels (the one that counts for the alert)

1. **Grade the reference days.** The claim judge, pinned to its frozen version, grades the five
   reference runs (431 judged answers) once, with the production settings, under `judge-v5`.
2. **Three checks before any label**, each costing nothing more:
   - its fail rate on the 431 is at most v2's (322 of 431, 74.7%) plus 5 points, so it cannot
     pass the false-pass bar just by failing more answers;
   - judge errors are at most 2% of the 431;
   - a leave-one-day-out run over the reference days with `judge_supported` in the family stays
     quiet, as v2's check required.
   If any fails, no label is drawn and the round ends.
3. **The draw.** For each reference day, *k* answers the claim judge passes and *k* it fails,
   seeded ("judge-check-v5-DAY"), from the 376 fresh answers, leaving out judge-error answers
   and both judges' dashboard lists. It excludes exactly the first check's 30 sampled answers,
   and is refused if any other label, under any labeler's name, exists on the reference runs
   (phase 7's rule, so no one can steer the pool by labeling). The sample is recorded write-once
   under the claim judge's keys, with *k*, the bars, and v2's 322 of 431 inside the record, and
   the score reads them only from there. *k* is 6 (60 labels) or 3 (30), decision 3.
4. **The labels.** The author labels them blind, with the decided rubric on screen and no judge
   verdict in view, in the `judge-check label` flow run with the claim judge's config.
5. **The bar:** false passes among the passes drawn, at most 4 of 30 (with 60 labels) or 2 of 15
   (with 30). If a day has fewer than *k* fresh passes, it gives all it has, and the bar is the
   largest count whose share of the passes drawn is at most 4 of 30 (2 of 15 with 30 labels).
6. **Reported, not gated:** both judges' rates of failing unsupported answers and passing
   supported ones, J, and the paired difference J(claim) minus J(v2), each weighted to the pool
   actually drawn from by day and stratum, with intervals from a stratified bootstrap that uses
   Wilson per-cell rates, so a cell with no error does not collapse to zero width. With *k* = 3
   these intervals are indicative only. The bar counts each day's passes alike; the estimates
   weight each day by its pool.

How likely the false-pass bar is to admit a judge, by its true false-pass rate among the answers
it passes (exact binomial, as if the passes were one sample; v2's rate on the first check was 4 of
15, 27%):

| True false-pass rate | 60 labels: at most 4 of 30 | 30 labels: at most 2 of 15 |
|---|---|---|
| 5% | 0.98 | 0.96 |
| 10% | 0.82 | 0.82 |
| 15% | 0.52 | 0.60 |
| 20% | 0.26 | 0.40 |
| 27% (v2's rate) | 0.06 | 0.19 |

Both sizes admit a good judge (10%) about 8 times in 10; the three pre-label checks are known
before any label is made. The 60-label check admits a judge as lenient as v2 about 1 time in 16;
the 30-label check about 1 time in 5. What this means for the alert: at the fail-rate cap, a
judge whose true false-pass share is above about 16% would have a lower J than v2's (the alert
would need a bigger true change to fire), and the 60-label bar still admits one at 20% about 1
time in 4 (the 30-label bar, 2 times in 5). The bars make a less sensitive judge unlikely, not
impossible; the reported J difference and its interval say how the claim judge actually
compares.

### Keeping it blind

The first check's guards are keyed to its two record names, so each needs extending before the
draw:

- `pending_blind` and `blind_cases` read every judge's sample records, and hold back every
  judge's grades on sampled answers (both judges' verdicts, reasons, `judge_support_score` and
  judge errors) in `trace`, `label --show-judge`, `judge-validate`, `judge-probe`, `compare` and
  `scripts/judge_compare_v5.py`. v2's verdicts are held back too, since they track the claim
  judge's.
- The verdict digests cover the claim judge's grades, so a re-judge after the draw is caught.
- No claim-judge dashboard is built before the score, and the draw leaves out both judges'
  dashboard lists.
- **Development work,** from approval (and in effect now) until the score, publishes only
  aggregates, and never reads the 376 fresh answers, their grade rows or their cache entries.
  What was read from the reference runs while this proposal was written: aggregates only (the
  share of v2's replies that think before the verdict, output tokens, cost per answer, and counts
  of table pipes and backticks in quotes per day); the first check's 30 sampled answers and their
  published labels; and the 25 answers the dashboard shows. None of the 376.

### What is built whatever the result, and what a pass or a fail changes

- **Built before stage A, and kept either way:** one pin per judge name (`judge` stays pinned to
  `2cf75fa03e09`; `judge-v5` is allowed to grade online runs, which is separate from being in the
  family, and is pinned to its frozen version before stage C); one judge check per judge on the
  same runs; the `judge_error` grade; and `monitor DAY` refusing a day already in `alerts.json`,
  so no pre-registered verdict can be overwritten. An ADR records these changes when they are
  built, and the round's result is added to it either way.
- **Pass:** the claim judge becomes the online judge. A write-once database record names it
  (grader name, version, config), written when its check passes; `online-run`, the monitor and
  the dashboard read it, and `online-run` judges with that judge's config while the agent runs
  with the day's config, so the champion's `configs/default.yaml` keeps v2 for offline use.
  Membership in the family is read at run time from the claim judge's check record, as it is for
  v2's, so the reference is not re-frozen and `alerts.json` is not archived. Days 6 to 11 are
  re-judged by the claim judge so the dashboard can show one judge (the pass-only cost below);
  until then the dashboard stays on v2. Any post-hoc reading, such as whether day 7, the honest
  miss, would have alerted, is written to its own file, never to `alerts.json`, and is reported
  as post hoc, never as a catch.
- **Fail (stage A, B or C):** `judge_supported` stays out of the family, v2 stays everywhere,
  `judge-v5` stays pinned but no online run uses it, no further revision is made in this round,
  and the miss is recorded here and in the ADR with all its numbers.

## What has to be built (after approval)

- `graders/judge_prompts.py`: the claim prompt, the skeptic prompt, their two tools (the
  skeptic's with per-item scores and a role field), the message templates without URLs, and the
  decided rubric under a new key; examples from dev-split cases only.
- `graders/judge.py`: the claim method beside the current one: normalization, span and coverage
  checks, flagged items, the skeptic call, retries for both calls, the `judge_error` grade, the
  three grades, the reason, and the version fingerprint above.
- `config.py` and `configs/judge-v5.yaml`: method, *t*, *s*, grader name `judge-v5`, the model
  named explicitly (a block without a name defaults to haiku), pricing filled in so commands
  print spend, and no responsive call.
- **Spend:** a cumulative spend record for this round, written by every paid command and script,
  and a stop before the total would pass the approved amount; a cap on `evals judge`,
  `evals judge-probe` and the script, which have none today.
- `online.py`, `cli.py` (`evals judge`, `online-run`, `blind run`, `monitor`) and `monitor.py`: the
  pin per judge name, checked by `evals judge`, `online-run` and `load_day`; the online-judge
  record and `online-run` judging with its config; `load_day` and the monitor accepting and
  counting `judge_error` grades, with the 2% untrusted rule; `monitor DAY` refusing a recorded
  day; and a separate file for post-hoc readings.
- `monitor.py`, `dashboard.py`, the `judge-check` command and `store/db.py`: the judge's name as a
  parameter where "judge" is hard-coded today; sample and check records per judge, the first
  check's left as they are, the write-once rule covering the new keys, and a run allowed one check
  per judge; the draw excluding exactly the first check's sample and still refusing any other
  label; *k*, the bars and sample sizes that can differ by day, read from the write-once sample
  record; the pre-label checks; published copies per judge; and the blindness guards above.
- `judge_probes.py`: probes scored on `judge_supported` only, a pool that needs no responsive
  verdict from the claim judge, and the hedge, supported-claims and swapped-service-name probes.
- `scripts/judge_compare_v5.py`: stage A's grid and choice and stage B's screen, the extra salts
  for both judges without storing grades, every number printed, with the blindness guards and the
  spend cap.
- Offline tests with the scripted fake model for each rule.

## Cost and human time

Judge v2 costs $0.0054 to $0.0062 per answer online (from the cache; $0.45 for day 1's 83). The
claim judge writes several times more output, and the skeptic adds a call on the answers that
pass at *t* 50 (30% to 60% on the dev runs) and on flagged answers. Estimate: about $0.02 per
answer, measured in the pilot first.

| Step | Calls | Estimate |
|---|---|---|
| Stage A: pilot, 20 answers under three salts | 60 | $1.20 |
| Stage A: up to four versions once, two candidates twice more | 500 + 500 | $20 |
| Stage A: v2 under two more salts | 250 | $1.50 |
| Stage B: the claim judge on 171 under three salts, v2 under two more | 513 + 342 | $12.50 |
| Stage B: grading the probe base and about 200 probe variants | about 315 | $7.50 |
| Stage C: the five reference days | 431 | $8.60 |
| **Before any label** | | **about $51 at the estimate; $45 to $60** |
| Only if stage C passes: re-judge days 6 to 11's served runs | about 940 | $17 to $22 |

A leaner run is possible, at about $30 before any label: single passes in stages A and B, with
every count cap widened by the ceiling of the pilot's flip rate times the number of answers the
cap counts, and stage A's choice and stops, and stage B's gates, read from single passes. It
trades noise protection for money.

After a pass, online days also cost more to judge: about $3 instead of $1 for a 200-question day.

Human time: about 15 minutes for the decisions, then the blind labels at stage C. The first
check's 30 labels took 27 minutes (02:16 to 02:43 UTC on 2026-10-21), so about 55 minutes for 60,
or about 30 for 30.

## Risks, and how the design meets them

- **Missed claims lead to false passes.** The coverage check makes every uncovered number or name
  a flagged item, which the skeptic must score as supported whatever *s* is.
- **The skeptic adds false fails.** Its general threshold is tuned against new false fails ("off"
  is a choice), stages A and B cap new strictness and hedge or advice regressions, and stage C
  caps the fail rate.
- **Spans that do not match.** Normalization covers the formats in the quotes; replies that fail
  are retried once and then become judge errors, which are counted, capped, and never read as a
  verdict; dropped spans are counted and capped.
- **Tuning to 125 labels.** Two knobs on a nine-cell grid, at most three revisions, a fixed order
  of choice with stops, gates on new errors plus one build check, and a fresh blind stage.
- **Noise.** Stage A's final choice and stage B use means over three passes for both judges;
  stage A's exploration of versions uses single passes. Stage C judges once, as production will,
  and its intervals are reported.
- **Small samples.** The power table is above, and decision 3 is about exactly this.
- **Cost.** The pilot measures it first, and a cumulative stop holds the round to the approved
  amount.

## Lessons from the earlier rounds, applied

- A rule of "at least as good as v2" on 30 labels is close to a coin flip when the judges differ
  by an answer or two (sections 8 to 10). Stage C uses absolute bars tied to what the alert needs,
  with the power shown, and the earlier stages gate only on new errors, on three-pass means.
- A fix for one error can create another (v2.1 and hedges). Hedges and advice are named as no
  claim in both prompts, and the named hedge and advice answers v2 gets right, a hedge probe, and
  new false fails are all gated.
- A change must be tested on labels collected after it is written (section 10). Only stage C's
  labels count as validation, and they are drawn after the judge is frozen.
- Core supported is advisory (kappa 0.37 on the 65 blind labels); only fully supported is gated
  (section 10's correction).
- The judge credited attributions from the URL: the claim judge does not see URLs.

## Open decisions

Each has a recommended default; together the defaults form one consistent plan.

1. **The design**, all three parts: the claim table with its code checks, the confidence scores
   with one tuned threshold, and the skeptic pass. Approval also covers up to three revisions of
   the claim judge's prompts or grading code during stage A, under the rules above. *Recommended.*
2. **The rubric.** (a) Rubric v4 plus the two paragraphs above. *Recommended.* (b) Rubric v2
   unchanged, with the consequences described in the rubric section (hedges judged as claims;
   the hedge probe and hedge regression rules reported, not gated).
3. **Fresh labels at stage C.** (a) 60 labels, about 55 minutes: a judge as lenient as v2 gets in
   about 1 time in 16, and one at 20% false passes about 1 time in 4. *Recommended.* (b) 30
   labels, about 30 minutes, as planned on 2026-10-21: those figures become 1 in 5 and 2 in 5.
4. **The bars.** At stage C: false passes at most 4 of 30 (or 2 of 15), and before any label a
   fail rate at most v2's plus 5 points, judge errors at most 2%, and a quiet leave-one-day-out
   run. In stages A and B: new errors only, plus the no-regression rule on named answers v2 gets
   right and one build check, that the frozen version beats v2 on the 19 named cases (v2 makes 9
   errors there). *Recommended.*
5. **Changes to approved phase 7 rules.** (a) One pin per judge name, so the claim judge may grade
   online runs before it is validated, without entering the family. (b) A second judge check on
   the same reference runs under the claim judge's own records; the first check's records stay
   untouched; "a run belongs to one judge check" becomes "one check per judge"; the draw leaves
   out exactly the first check's 30 answers and is still refused if any other label exists. (c)
   Judge errors become their own grade, kept out of the rates and capped. (d) `monitor DAY`
   refuses a day already recorded. (e) Online-split cases are used neither as tuning labels nor
   as prompt or rubric examples. *Recommended.*
6. **Scope of a pass.** Online only: the claim judge joins the alert, v2 stays the offline judge,
   and the claim judge's offline numbers are the stage A and B numbers on the dev runs' labeled
   answers, reported beside v2's at no extra cost. Moving the offline loop to it would restate
   every published number, and is a separate decision. *Recommended.*
7. **Budget.** (a) About $45 to $60 before any label, with the pilot first, a stop if the cost per
   answer runs more than 1.5 times the estimate, and a cumulative stop at the approved amount;
   then, only on a pass, about $17 to $22 to re-judge days 6 to 11, and about $3 instead of $1 a
   day for online judging after that. *Recommended.* (b) The leaner run, about $30 before any
   label, with single passes and caps widened as stated above.

## Appendix: named cases (frozen 2026-10-28)

Cases this document quotes, paraphrases, or built a pattern from, by id: online-d01-085,
online-d02-071, online-d03-068, online-d04-030, online-d04-032, online-d04-072, docs-ai-001-p2,
docs-ai-004-p3, docs-ai-007, docs-chg-004, docs-chg-007, docs-chg-008, docs-chg-022,
docs-core-025, docs-core-034, docs-int-014, docs-int-020, docs-int-029, docs-mix-022,
docs-mix-024, docs-mix-049, docs-mix-050, docs-mix-057, docs-mix-078, docs-mix-090. The later ones
are the cases behind the skeptic's patterns (named sources, terms only in the page text,
headerless table rows), from `docs/judge-validation.md` and the label notes.

On the tuning set they are 19 answers (4 labeled supported, 1 of which v2 passes), holding all 9
of v2's errors there; on the screen, 29 answers (7 labeled supported, 4 of which v2 passes),
holding 9 of v2's 11.

## How the first review was taken in

| Finding (severity) | Change |
|---|---|
| Stage B's "held-out" labels already shaped the design (high, both reviewers) | Stage B is a regression screen that gates only on new errors; false passes there are reported, not evidence; named cases are listed |
| Stage B's bars could be met by fixing the named cases (high) | False passes are no longer gated before stage C; new false fails are capped |
| Stage C's bars did not bound what the alert needs; failing more could pass (high) | A fail-rate cap against v2 before the draw; J and both judges' rates estimated and reported |
| Stage C's blindness guards were not named (high) | "Keeping it blind", and the matching build items |
| A 2% error allowance would leave days unreadable by the monitor (high, both) | Retried, then a `judge_error` grade the monitor accepts and counts (revised again in round two) |
| *t* contradicted the rubric (medium, two) | The score is the judge's confidence under the rubric, so *t* is a calibration choice |
| Code-checked spans and coverage were weaker than claimed (medium, two) | Spans of at least three words, number cross-checks, factual-token coverage, `not_claims` checked; the audit claim narrowed |
| `judge_support_score` invited post-hoc thresholds (medium, two) | Reported for audit only; any other *t* is a new judge |
| Unpinned choices; one pass per stage (medium) | Ties, stops, revisions and short days fixed; three-pass means |
| The pin, the build list and the pass path (medium) | Per-judge pin before stage A; build list extended; no re-freeze; post-hoc readings in their own file |
| Decisions did not cover the approved phase 7 rules (medium) | Decision 5 |
| Coverage changed no verdict with the skeptic off; the admission chances; stage A's cap; the probe bar; cost; and six low findings | All corrected |

## How the second review was taken in

| Finding (severity) | Change |
|---|---|
| Stage A's stop was met by the named cases alone: all of v2's tuning errors are named (high) | Stages A and B gate on errors outside the named cases; the named cases rank candidates as the build working, not as evidence; "In short" says the old labels cannot show improvement |
| Stage A chose on single passes; its caps sat inside the noise (medium, two) | Two candidates re-judged under three salts, with v2 too; the choice and the eligibility use the means; the stops are explicit |
| The bars do not rule out a less sensitive judge (medium) | The probability is stated instead, in the text and decision 3 |
| The caps could not see a hedge regression (medium) | A rule on the named hedge and advice answers (revised in round three); a gated hedge probe |
| The forced-check rule contradicted itself (medium, two) | Flagged items are re-scored on the table's scale and pass only at *t*; one score per item; the skeptic's request never contains *t* or *s* |
| Stage C's estimand was ambiguous (medium) | Weighted to the pool drawn from, a paired J difference, Wilson per-cell intervals, indicative at *k* = 3 |
| Judge errors read as fails (medium) | Their own grade, out of the rates, left out of the draw, capped at stage C, and a day with over 2% errors marks the judge untrusted |
| Excluding labeled answers reopened the labeling lever (medium) | Only the first check's 30 are excluded; any other label still refuses the draw |
| Re-judged days could overwrite recorded verdicts (medium) | `monitor DAY` refuses a recorded day, built before stage A |
| Extra salts had no safe way to run (medium) | In the script, never stored, never under "judge" |
| The named list could grow during stage A (medium) | Frozen now; later-quoted cases stay in their counts |
| The blindness rule started too late (medium) | In effect from now, with what was already read recorded |
| The online judge's config and record had no home (medium) | A write-once record names the online judge, and `online-run` judges with its config |
| Prompts used online-split cases (medium) | Dev-split examples only; the advice example is now docs-chg-004's |
| Probe pool, *k* in the record, short days, the leave-one-day-out timing, the budget stop, J's pool, the ADR, and the min-over-claims length effect (low) | All fixed in the text above |

## How the third review was taken in

| Finding (severity) | Change |
|---|---|
| Stage A's cap on the 4 named answers labeled supported demanded passing table rows v2 fails, so v2 itself was ineligible (high) | The rule now covers only named answers labeled supported that v2 also passes (1 in tuning, 4 in the screen): a pure no-regression rule; the others are reported as built in |
| "Only new errors are gated" contradicted gates that required fixing v2's errors (medium) | Those gates are now no-regression rules; the one remaining improvement requirement, the build check, is stated in the text and decision 4 |
| Rubric v2 (decision 2(b)) conflicted with the hedge rules (medium) | Its consequences are spelled out: hedges judged as claims, and the hedge probe and hedge rules reported rather than gated |
| The named-case stop could let a non-improving frozen version through (low) | The stop now tests the frozen version on its means |
| A failed skeptic reply had no error path (low) | Both calls share one retry and the `judge_error` path; a retry replaces the first reply in full |
| Parts 1 and 2 disagreed on a claim that loses one of several spans (low) | One rule: a claim with no valid support entry is unsupported |
| The leaner run had no widening rule (low) | Each count cap widens by the ceiling of the pilot's flip rate times the answers it counts |
| "In short" overstated where v2's errors sit (low) | Narrowed to all false passes, and all errors on the tuning set |
