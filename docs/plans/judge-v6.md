# Proposal: judge v6, the second claim-judge round

**Summary.** The claim judge (v5) stopped at its first validation stage on 2026-10-29: over three
passes on 106 tuning answers it made 9 errors against a cap of 8, one error over the three passes.
Four answers made all nine, and a review of the saved records traced them to four mechanisms: a
name no quote states passed on a round score at the threshold, a type name scored across the
threshold between samples, a hedge that named the service failed because two calls had to agree,
and support carried only by a link target the judge is not shown. This proposal keeps v5's
architecture (a claim table that code checks, a confidence score per claim, a skeptic pass) and
changes two things in the judge and two in the process. In the judge: each claim gets a gap type,
two of which fail the claim whatever its score, and code clears the cited service's own name
inside a hedge. In the process: every version is judged under three fresh samples and the choice
is made on three more, and the four answers the fixes were written from, with one case that names
their risk, leave the count that decides eligibility. The fourth mechanism, the link target, is
not fixed; that answer is pre-registered as a known miss. Every change was written from v5's
tuning data, so none of it is evidence; the validation rules are fixed here, before any paid
step, and only the fresh blind labels of stage C count. Estimated cost about $44 before any
label, capped at $60. **Status: proposed 2026-10-29, awaiting approval.**

The design and every number below come from the records of the v5 round (`results/judge-v5/`
and the result section of `docs/plans/judge-v5.md`). Nothing was judged for this proposal: no
paid call was made, and no answer outside the 125 tuning answers was read. Every figure quoted
from the records is recomputed by `scripts/judge_v6_figures.py` into
`results/judge-v5/analysis/figures.json`; the blind classification of the judge's reasons is in
`results/judge-v5/analysis/classes.json`, and the selection model in
`scripts/judge_v5_selection_model.py` (`results/judge-v5/analysis/README.md` lists them). Two
kinds of figure are human readings rather than recomputations, and are marked where they appear:
the classes given to the judge's reasons (both readers' classes are in `classes.json`, and the
rules simulated on them are in `figures.json`) and one analyst's matching of the skeptic's spans
to the label notes. Every such figure is post hoc: it re-reads replies already saved, and a
changed judge would reply differently.

"Judge v6" is the judge's own version number. It has nothing to do with the docs agent's prompt
versions. Where this document says "v5" it means the claim judge as frozen at the end of its
round (revision 1, commit 7a0231a); "v2" is the validated judge that grades every run today. The
**build check** is v5's stage A rule that the frozen version must make fewer errors than v2 on
the named cases; the **kept rule** is its rule that named answers labeled supported which v2
passes must still be passed.

## In short

- **Why.** v5's mechanism worked: no retries, no judge errors, dropped spans never flipped a
  verdict, and it cost $0.0105 per answer, half the estimate. It missed its bar by one error over
  three passes: 3.0 errors per pass against a cap of 2.67, and 1.67 false fails against 1.33.
  `judge_supported` is still out of the online alert, and the 376 fresh answers kept for a blind
  test are still unread.
- **What changes in the process.** Each version is judged under three fresh samples (salts), never
  one, and eligibility is read on their means. The two candidates are then judged under three more
  fresh salts, and admission is decided on those, so a version cannot be admitted on the passes
  that made it look good; the order between the two candidates still comes from the exploration
  passes. v5's salt names are never reused: under a reused name the call cache replays the old
  reply, and the pass is not a new sample.
- **What changes in the count.** v6 freezes its own list of the cases this document quotes, as v5
  did for its text. The four answers the fixes were written from and the one case that names
  their risk join v5's 25 cases, by case id, which moves 13 answers: 93 answers decide eligibility
  (v5: 106) and 32 are named (v5: 19). On the 93, v2 makes 2 errors over its three passes (its
  stored pass none), so the caps are 2.67 errors and 1.33 false fails per pass, as in v5. The
  build check becomes the gate that bites: on v5's saved passes revision 1 makes no error on the
  93 and 8.33 errors per pass on the 32, against v2's 7.33. The kept set grows from one answer to
  seven, one of them pre-registered as a known miss. Every gate is also reported on v5's split,
  for continuity; such a figure decides nothing.
- **What changes in the judge.** (1) Each claim carries a gap type: none, implication, scope, a
  term in no quote, a headerless table cell, other. A term in no quote and a headerless cell fail
  the claim at every threshold; the other types are scored as before. Code downgrades a term that
  is the cited service's own name or the publisher's, since the judge is not told which service
  the quotes come from, and records, without deciding, whether a quote holds the copied term.
  (2) Code clears the cited service's own name when it is flagged inside words that make no
  claim, unless those words name a source. (3) The grid is read as the score levels the judge
  uses: *t* in {64, 69, 74}, the levels 65, 70 and 75, and *s* in {off, 49, 59}, the levels 50
  and 60. (4) Names inside claims that no quote states are counted and reported, never used to
  decide.
- **What is evidence and what is not.** Changes 1 and 2 were written from three of the four
  answers behind v5's stop, and on the saved passes each fixes the answers it came from
  (docs-chg-010 and docs-mix-042; docs-ai-003-p3). Change 3 changes no saved verdict, change 4
  decides nothing, and nothing fixes docs-mix-058, which is pre-registered as a miss. That the
  fixes fix their own cases is the build working, not an improvement; such answers are named, and
  only stage C's fresh blind labels can show an improvement. Stages A and B gate on new errors, as
  in v5.
- **Cost.** $39 to $41 at the measured $0.0105 to $0.0111 per answer, about $44 at the $0.012 the
  budget assumes (the gap field adds a little output), capped at $60 in a new ledger; $60 is the
  cap, not the estimate. Then 60 blind labels, about 55 minutes. v5's round spent $6.95 of its own
  $60.

## What v5's records show

Stage A of v5 judged 20 answers under three salts (the pilot), the first draft once on the 125
tuning answers, revision 1 once, and then revision 1 and v2 under two more salts each. Four
answers made all 9 of revision 1's errors outside v5's named cases over the three passes:

| Answer | Label | v2 | Revision 1 at t = 70, three salts | What happened |
|---|---|---|---|---|
| docs-chg-010, the champion's run | not supported | fail | pass, pass, pass | "its Harness service lets you define and invoke an agent with a single API call" scored 70, 72 and 70, while the judge's own reason said no quote names Harness |
| docs-mix-042, iteration 4's run | not supported | fail | fail, fail, pass | its weakest claim, which reads a NAT gateway type no quote states, scored 50, 60 and 70 |
| docs-ai-003-p3, the champion's run | supported | pass | pass, fail, fail | the hedge "this applies to Bedrock cross-Region inference specifically; I did not check other AWS services" was listed as no claim, code flagged the name Bedrock in it, and the skeptic cleared it once in three |
| docs-mix-058, iteration 4's run | supported | pass | fail, fail, fail | "adjustable through Service Quotas" is carried only by a link target inside the quote, which the judge is not shown |

What the saved passes show about each mechanism. Unless a scope is given, a figure is over
revision 1's 375 judgings (125 answers under three salts, named cases included):

- **Scores come as levels.** Of the 548 claim scores in the band that decides verdicts (50 to 85),
  523 are multiples of 5; the others are 72 (16 times), 82 (3), 68 (2), 58 (2), 78 (1) and 62 (1).
  The weakest claim of a judging sits at 65 in 5 judgings, at 70 in 12, at 72 in 5 and at 75 in 3.
  Outside v5's named cases the 70 level holds both labels: 4 judgings of unsupported answers and 3
  of supported ones; six of the seven are on cases v6 names, and outside the 32 the level holds one
  supported judging. No threshold separates them: on the 106, over three passes, t from 66 to 70
  gives 4 false passes and 5 false fails in total; 71 to 72, 0 and 8; higher, 0 and 11 or more.
- **The judge's reasons separate what the score ties.** The unsupported judgings at 70 give a name
  or term no quote states ("Harness", the NAT gateway type); the supported ones give a mild
  rewording or an implied scope. Read blind to the labels (the rule in `classes.json`), the
  reasons of the weakest claims scored 50 to 85 (115 claims, over 106 of the 375 judgings) sort
  into: stated outright (16, all on supported answers), plain rewording (6: 4 supported, 2 not),
  implication (41: 17 supported, 24 not), a name or term in no quote (27: 3 supported, 24 not), a
  qualifier or scope beyond the quote (14: 6 supported, 8 not), a headerless table cell (10, all
  unsupported), and a claim that depends on the current date (1, unsupported). A second reader,
  also blind, gave the same class on 108 of the 115 (112 when either reader's alternative reading
  counts). The other claims of the judgings revision 1 passes at the 70 level were read too: 80
  classified blind and 204 screened as stated or reworded; of the 80, one was a term in no quote
  (the Harness claim, under the salt where another claim of its answer scored lower) and none a
  headerless cell.
- **A rule on two of those types removes the false passes without adding false fails,** on the
  saved passes and the classified claims (the simulation is in `figures.json`): failing every
  claim whose reason names a term in no quote or a headerless cell, at t = 70, gives 0 false
  passes and 5 false fails outside v5's named cases (from 4 and 5). Categories alone do worse:
  passing only "stated" and "plain rewording" gives 0 and 14.
- **The skeptic finds the right claim but is unsure where it matters.** Outside v5's named cases,
  its search names the part the label note calls unsupported in 102 of its 126 searches on
  unsupported answers (one analyst's reading of the notes), and in every false pass it names the
  claim at issue. Its confidence on those false passes is 50, 55, 55
  (docs-chg-010, the champion's run) and 60 (docs-mix-042); on supported answers that pass at
  t = 70 it reaches 45, on docs-chg-010 on iteration 4's run, a kept answer (40 under the other two
  salts); outside v6's named cases it reaches 30. So *s* at 50 would have removed all 4 false
  passes and added none, and *s* at 70 or 90, v5's grid, never changed a verdict at t = 70 (at
  t = 50, s = 70 did). The *s* cells of v6 rest on this one 5-point gap, which only the fresh
  passes and the kept rule test.
- **The service's own name.** Ignoring the flag on "Bedrock" inside docs-ai-003-p3's hedge at
  verdict time changes 2 of the 375 verdicts, both that answer under the two salts where the
  skeptic did not clear it, both to pass.
- **Links in quotes.** On the 125 answers, 18 quotes hold 19 markdown links: 17 to documentation
  pages, whose visible text already names the page, 1 to a pricing page, and 1 to the AWS console
  (docs-mix-058). In the snapshot, 3,389 of the 3,570 console links are "Yes" cells on quota
  pages. Showing console targets would reopen the channel v2 was credited through
  (`docs/judge-validation.md`, section 9) for one answer's sake; the proposal does not.
- **Selection on the exploration passes is optimistic.** In a simulation
  (`scripts/judge_v5_selection_model.py`: each answer outside v5's named cases errs with the share
  of revision 1's three passes in which it erred at t = 70, so four answers err, at rates 1.0,
  1.0, 0.67 and 0.33; every other answer never errs; v2 is fixed at its three-pass means; the
  versions are independent and equally good; seed 20261029, 200,000 rounds), a version that truly
  sits just over the caps is admitted 63% of the time when four versions are explored on one pass
  each and the choice reads those passes, 59% on three passes each, and 12% when the chosen one
  is re-judged on three fresh passes. The model describes v5's count: on the 93 revision 1 flips
  no verdict at the 70 level.
- **The rest of the records.** Over the 540 distinct judgings of the round: no retries, no judge
  errors, 31 support entries dropped, every one for being under three words, none flipping a
  verdict; the skeptic's span was always in the answer. Cost $0.0105 to $0.0111 per answer, 71% to
  74% of it output tokens; v2 cost $0.0048 per answer.

## The design

### What stays the same

Everything in v5's "What stays the same", parts 1 to 3 and "Judge errors" holds, with the
changes below. The model, the input (question, answer, numbered quotes without URLs or link
targets), the span and coverage checks, the number cross-check, the skeptic's flagged items and
search, the retries, the `judge_error` grade and the three grades are unchanged. Rubric v5's text
is unchanged (decision 3). The version fingerprint gains the gap rule's code version, the
source-word list, the generic-word list and the service-name file's hash. The judge runs under its
own grader name, `judge-v6`, never under "judge"; v2 stays the validated judge everywhere unless
stage C passes.

### Change 1: a gap type per claim

The claim table gains one field per claim, `gap`, from a fixed list, and a `term` field that is
empty unless the gap is a term:

| Gap | Meaning, as the prompt states it | Effect |
|---|---|---|
| `none` | a quote states the claim, or it follows by simple arithmetic, a unit conversion or plain rewording | scored, as in v5 |
| `implication` | the claim follows from the quotes only by an inference | scored |
| `scope` | the claim strengthens or widens what a quote says: a qualifier, a scope, a default | scored |
| `term` | the claim names a feature, product, API, parameter, version, edition, Region, pricing model or other term that neither the quotes nor the question uses, in any form; the judge copies the term | unsupported at every *t* |
| `cell` | the claim reads a table cell as a default, maximum, limit, date or the like, and no quoted header names that column | unsupported at every *t* |
| `other` | any other gap | scored |

The prompt states two things about `term` that need no knowledge beyond its input: the publisher
(AWS, Amazon, or the "we" of a quote) is never such a term, and a word is in a quote when the
quote has it in another form (a plural, an abbreviation a quote defines, a different case). The
judge is never told which service the cited pages document (its input has no URL), so the labels'
other exclusion, the service's own name in any form (Bedrock or Amazon Bedrock, SageMaker or
SageMaker AI), is applied in code: a `term` whose copied term is a listed name of a service the
answer cites, or of the publisher, is downgraded to `other` and scored as v5 would score it. The
downgrades are counted and reported.

Code also records, for every `term`, whether the copied term occurs in a quote or the question by
the grader's normalized match. That record never decides, in either direction: a lexical hit can
be another sense (docs-mix-042's NAT gateway type "Private" against "private subnets" in a quote),
and a purely lexical rule on v5's passes gives 1 false pass and 18 false fails, from names written
another way. Stage C reports how often the rule was right on fresh labels. The skeptic's search
carries the same field, recorded and reported, and still decides only by its confidence against
*s*.

### Change 2: code clears the cited service's own name inside words of no claim

v5 flags every capitalized name inside a `not_claims` span and sends it to the skeptic, which
clears it by scoring it as no claim. On the service's own name the skeptic did so once in three on
the one answer where it mattered. In v6, code ignores such a flag at verdict time when:

- the flagged word lies inside an occurrence, in the normalized span, of the whole of one listed
  name of a service the answer cites, or of the publisher (AWS, Amazon). A one-word name matches
  one word; a word of a longer name does not clear on its own, so "Control" in "Service Control
  Policies" is not cleared by a name that contains "Control"; and
- the `not_claims` span holds no source word: page, guide, docs, documentation, reference, FAQ,
  blog, post, article, whitepaper, announcement, notes, notice, overview, example, tutorial,
  comparison, table, section, list, limits, quota, quotas, pricing, console. A note that names a
  source stays with the skeptic, since two of v2's online false passes were statements about a
  source whose name held a service's name. The list is frozen in the rules data, and the spans
  cleared are reported at every stage, so a miss is visible.

The flag stays in the skeptic's request and the skeptic still scores it; code ignores that score
when the rule applies and records it beside the cleared flag. So the skeptic's request, and its
cache key, never change because of this rule, and the no-call check below is exact.

The service names come from the docs snapshot (every page title ends in " - " and the guide's
name; the 13 pages under `general/` name the service before "endpoints and quotas"; a title that
names several services is split on commas and "and"; a suffix such as "(AWS CDK) v2" is dropped;
the short form drops "Amazon " or "AWS "; an abbreviation a service's pages define in parentheses
after the full name is added), plus a hand list for short forms the pages never define (ELB,
OpenSearch), generated by a committed script into a committed data file, each name marked by its
source, covering all 103 path segments. Beyond the 26 guides the tuning answers cite the names
rest on titles alone; a missed name only sends the flag to the skeptic, as in v5. The file is
part of the judge's version. A name inside a claim's `answer_span`, and an uncovered name, are
flagged as in v5.

### Change 3: the grid, read as levels

*t* is chosen from {64, 69, 74} and *s* from {off, 49, 59}. The judge's scores in the deciding
band are multiples of 5, so each value sits just below a level and the three cells are the levels
65, 70 and 75 (for *s*: 50 and 60); no claim in v5's records scored 64, 69 or 74. On v5's
saved passes t = 64, 69 and 74 give the same verdicts as 65, 70 and 75 on all 375 judgings, so
this change changes no saved verdict; v5's fault at the 70 level is addressed by the gap rule
(change 1), not by moving *t*. What is new is the grid's reach: v5's cells at 50 and 85 were far
from any eligible cell (on the single first pass, t = 50 gave 14 false passes for the draft and
16 for revision 1, and t = 85 gave 7 false fails for both, on the 106), and its 70 and 90 for *s*
never changed a verdict at t = 70. The 75-level cell is live for fresh data only: on the saved
passes it fails the kept rule through docs-chg-010 on iteration 4's run, whose weakest claim
scored 70, 70 and 72.

### Change 4: names inside claims, counted only

For every claim, code counts the capitalized names inside its `answer_span` (as the coverage check
finds them) that occur in none of the answer's quotes, are not a listed name of a cited service or
the publisher, and are not on a fixed list of generic words (Region, console, API, CLI, SDK and
the like, frozen in the rules data). The count is reported at every stage and recorded beside each
fresh label in stage C. It never decides: on v5's passes it would fix one judging and break one,
the acronym's plural on docs-ai-003-p1 where the quote has the singular.

### What the grader records

As v5, plus per judging: each claim's gap type and term, whether a quote holds the term, the
downgrades of change 1, change 2's cleared names with the skeptic's score on each, and change
4's count. `evals judge` prints the counts and adds them to the run note.

## Validation, with the rules fixed now

The data, its limits, and stages B and C are v5's, with the named-case counts below. What
changes is stage A's process and its counts.

### Named cases and the counts

v5's rule stands: every case a design quotes, paraphrases or built a pattern from is named, by
case id, and named answers stay out of the count that decides eligibility. v6 freezes its own
list now, before any v6 pass: v5's 25 cases plus the four answers' cases, docs-ai-003-p3,
docs-chg-010, docs-mix-042 and docs-mix-058, and docs-ai-003-p1, whose quote has an acronym's
singular where the answer has its plural, the `term` rule's named risk. This re-places
docs-ai-003-p3, which v5's revision 1 quoted but left in the 106 under v5's later-revision rule.
A case a later v6 revision quotes stays in whichever count it is in today and is listed as added.

On the tuning set (the 125 judge-dev labels with citations on the three dev runs), this moves 13
answers, since a named case moves every run's answer on it:

| | v5 | v6 |
|---|---|---|
| Answers deciding eligibility (outside the named cases) | 106 (30 labeled supported) | **93 (24 labeled supported)** |
| v2's errors there over its three passes (stored, judge-s2, judge-s3) | 0, 1, 1 | 0, 1, 1 |
| The caps per pass (v2's mean plus 2 errors, plus 1 false fail) | 2.67 and 1.33 | **2.67 and 1.33** |
| Named answers | 19 | **32** |
| v2's errors there over its three passes | 9, 7, 6 (7.33 per pass) | 9, 7, 6 (7.33 per pass) |
| Kept set (named, labeled supported, passed by v2's stored verdict) | 1 | **7**, one pre-registered as a known miss |

v2's two extra passes, replayed from the call cache with no API call
(`results/judge-v5/analysis/v2-extra-passes.json`), made one false pass and one false fail on
the 106, both on answers that stay in the 93, and none on the 13 moved answers; their totals
match the choice record (1 and 1 on the 106; 12 false passes and 10 false fails on the 19).

The kept set: docs-chg-008 on iteration 4's run (v5's), docs-ai-003-p1 on the champion's and
iteration 4's runs, docs-ai-003-p3 on both, docs-chg-010 on iteration 4's run, and docs-mix-058
on iteration 4's run. **docs-mix-058 is pre-registered as a known miss** (decision 2): its
support is carried by a link target that the design withholds from the judge, so passing it would
mean showing URLs, which the v2 lesson rules out. It is reported at every stage and exempt from
the kept rule; the other six must each pass on at least two of three passes.

On the screen (stage B, the 171 judge-test labels) nothing moves: none of the five cases has a
judge-test label. Its counts stay v5's: 29 named answers (v2: 6 false passes, 3 false fails; 4
kept), 142 others (55 labeled supported; v2: 2 false fails, no false pass).

**How this reads.** The four answers behind v5's stop leave the eligibility count right after
that stop, and on v5's saved passes revision 1 makes no error on the 93 by construction. The gate
that then bites is the build check: on those passes revision 1 makes 25 errors on the 32 over
three passes, 8.33 per pass, against v2's 22, 7.33 per pass, so v5's stopped version fails v6's
stage A too, by one error per pass. With changes 1 and 2 fixing the answers they came from, the
projected figure is 19 (6.33 per pass), one error per pass under the bar, one of which
docs-mix-058 costs by design; the figure is fitted, and a v6 judge replies afresh. What keeps the
gate honest: the rule predates the result; the list is frozen before any v6 pass; the moved
answers stay in the build check and six of them join the kept rule; every gate is also reported on
v5's split (106 and 19), where a figure can neither stop the round nor rescue a stop (on that
split revision 1 passes the build check, 5.33 against 7.33); and stage C is the only evidence.

### Stage A: tune on fresh samples (no new labels)

**Salts and versions.** Salts are named in advance and never reused from v5: `judge-v6-LABEL-1`
to `-3` for a version's three passes, `judge-v6-choice-1` to `-3` for the choice. v5's script
rules carry over: one label per version fingerprint, a second label for the same fingerprint
refused (so a version cannot be re-drawn under a new name); a version is judged once under each of
its salts; no version is judged after the choice; the choice is made once. The choice step refuses
to proceed if any claim-judge call under a choice salt is served by the cache, and its ledger
entry must show uncached calls equal to calls, so no choice pass can be read before the choice.
v2's three passes are its stored verdicts and the two replayed passes above, read by a client that
refuses any miss; the replayed totals must equal the choice record's or the step refuses, and on
a miss v2 is judged under two new named salts (`judge-v6-v2-1`, `-2`) and the measured figure
replaces the replayed one.

1. **Pilot:** 20 answers from the tuning set, v5's draw rule with the seed `judge-v6-pilot`,
   judged by the first draft (label `draft`) under its three salts, to measure cost per answer.
   If it is more than 1.3 times $0.012, the work stops and the budget is revisited (at that cost
   the planned judgings would reach about $57; v5's stop factor of 1.5 would admit a cost at which
   they pass the cap). The 60 judgings count among the draft's passes (step 2), so the pilot costs
   nothing extra; a draft changed after the pilot is a revision, and the pilot's version counts
   among the four. The pilot measures the draft, whose prompts are v5's; revision 1's cost per
   answer is reported against $0.012 after its first pass, and the same stop applies to it.
2. **Each version is judged under its three salts on all 125.** A version is the first draft or
   one of at most three revisions; any change to the prompts, the tools, the message templates,
   the grading code, the service-name file or the word lists counts as a revision, and each is
   made from the tuning labels only. The grid is read from the three passes, as means (exact
   fractions), against v2's three passes on the same answers.
3. **Eligible cells:** on the 93, mean errors at most v2's mean plus 2, of which mean false fails
   at most v2's plus 1; each of the six kept answers passes on at least two of the three passes;
   and judge errors are at most 2% of the 125 in every pass. A judge error is no verdict and is
   neither kind of error here; on the named cases it counts as an error, as in v5.
4. **Candidates:** among the eligible cells of all versions, the two best by fewest errors on the
   32 named (judge errors included; the build working, not evidence), then fewer errors on the
   93, then lower *t*, then *s* off before 59 before 49. The second candidate is the first cell
   after the first in that order that differs from it in version or in *t*; if none does, the
   choice runs with one candidate. v5's two candidates differed only in *s* and gave the same
   verdict on all 375 judgings. On the saved passes the 75-level cells fail the kept rule, so the
   live cells are expected at the 65 and 70 levels unless the judge's scores move.
5. **Choice on fresh passes:** the candidates are judged under the three choice salts, and the
   grid cells they name are read on those means against v2's three passes. The frozen version is
   the first in step 4's order that is eligible on the fresh means, kept rule and judge-error cap
   included. Nothing from the exploration passes enters the choice except the order.
6. **Stops:** the round stops, with the miss recorded, if no cell of any version is eligible
   after the last revision, if neither candidate is eligible on its fresh means, or if the frozen
   version does not make fewer errors than v2 on the 32 named cases on its fresh means.

Reported at every step, gated or not: per-pass errors beside the means (v5's single pass hid a fix
that held on one salt of three); the same gates read on v5's split; each cell's fail rate on the
125 beside v2's 70.4%, read before stage B is paid for (v6 adds only mechanisms that fail more
answers, and stage C caps the fail rate at v2's plus 5 points: on the saved passes revision 1
fails 71% to 73% at the 70 level and 76% to 77% at the 75 level); flips between salts; the gap
types given; the term records and downgrades; the cleared names; the names counts; dropped
spans; retries; judge errors.

**The versions, as planned.** The first draft is code only, on v5's prompts: changes 2, 3 and 4
and the process. Revision 1 adds change 1 (the gap types), which rewrites the claim prompt and
tool, so every claim table is a new sample. Two more revisions may follow from the tuning labels.
The order isolates the prompt change: if the draft is eligible and revision 1 is not, the gap
types hurt, and the draft can still be frozen. Before any paid pass, the draft's verdict code is
run over v5's saved passes with no API call, and must reproduce these figures: at the 70 level,
any *s*, 0 errors on the 93 in every pass; at the 65 level 2 false passes over the three passes
(docs-chg-013 on iteration 4's run under two salts); at the 75 level 3 false fails (docs-mix-055
on the first dev run under two salts, docs-core-009 on iteration 4's run under one);
docs-ai-003-p3's two verdicts moved to pass at every cell; and the kept table per cell
(docs-chg-010 on iteration 4's run 3 of 3 at the 65 and 70 levels and 0 of 3 at 75; docs-mix-058
0 of 3 everywhere; the other five 3 of 3).

### Stage B: the regression screen, unchanged

v5's stage B, scored once after the choice, with the frozen judge under three fresh salts
(`judge-v6-screen-1` to `-3`) and v2 under two more (`judge-v6-screen-v2-1`, `-2`), its stored
verdicts as the first pass. The four gates and the probes are v5's, with the counts above (142
others, v2's 2 false fails; the 4 kept answers; judge errors and dropped-span flips at most 2% in
every pass). The names count, the term records and the downgrades are reported beside the probes.

### Stage C: the online check on fresh blind labels, unchanged

v5's stage C: the frozen judge, pinned under `judge-v6`, grades the five reference runs (431
answers) once; the three pre-label checks (fail rate at most v2's 74.7% plus 5 points, judge
errors at most 2%, a quiet leave-one-day-out run); the seeded draw of 60 fresh answers (6 the
judge passes and 6 it fails per day) from the 376 never labeled, excluding the first check's 30
and both judges' dashboard lists, refused if any other label exists; the 60 hand-labeled blind;
and the bar, false passes at most 4 of 30 among the passes drawn. The power table and the reported
estimates are v5's. Two additions, reported and not gated: for each labeled answer, the gap types
the judge gave and whether its terms were in a quote, and the names count, so the rules' hit rates
on fresh labels are known at the end.

### Keeping it blind

v5's rules hold, and held through the analysis for this proposal: the 376 fresh answers, their
grade rows and their cache entries were not read; the analysis read the 125 tuning answers, their
labels, and counts only from the screen. The service-name file is built from the docs snapshot,
not from any answer.

### What a pass or a fail changes

As v5: on a pass, `judge-v6` becomes the online judge through a write-once record, days 6 to 11
are re-judged (about 940 answers, about $11), and v2 stays the offline judge; on a fail,
`judge_supported` stays out of the alert family, v2 stays everywhere, and the miss is recorded
here and in ADR 0009.

## What has to be built

- `graders/judge_prompts.py`: the gap field and term in `submit_claims` and in the search, their
  definitions and the publisher and other-form notes in both prompts.
- `graders/claim_judge.py`: the gap rule in `verdict`; the downgrade and the term record; the
  verdict-time clearing with the source-word list; the names count with the generic-word list;
  `T_GRID` and `S_GRID`; a new grading code version; the word lists and the service-name file's
  hash in the fingerprint; the new counts in the run note.
- `scripts/service_names.py` and `configs/service-names.json`: the map, by source.
- `configs/judge-v6.yaml` and the grader name `judge-v6`; `online.py`: the `judge-v6` pin at the
  freeze.
- `spend.py`: the ledger path and the pilot folder as parameters of the round
  (`results/judge-v6/`), cap $60, estimate $0.012, stop factor 1.3.
- The stage A script, as a v6 copy of `scripts/judge_compare_v5.py`: salts per version and for
  the choice, three passes per version, one label per fingerprint, v2's replays from the cache with
  a client that refuses a miss and saves per-answer verdicts, the replayed-totals check, the new
  named list and kept set with the pre-registered miss, the judge-error cap, candidates that differ
  in version or *t*, the choice on fresh means with the cache-hit refusal, both splits and the fail
  rates in every report, the pilot folded into the draft and its stop factor; then stage B (a
  partial build exists outside the repository and is reviewed before use) and stage C's draw,
  score, pre-label checks and records under `judge-v6`.
- Offline tests with the scripted fake model for each rule, and the no-call replay check above as
  a test over the saved passes.

## Cost and human time

At $0.012 per claim-judge answer (budget) and $0.005 per v2 answer:

| Step | Claim-judge judgings | v2 judgings | Estimate |
|---|---|---|---|
| Stage A: four versions, three passes each, on the 125 (pilot included) | 1,500 | 0 (replayed) | $18.00 |
| Stage A: two candidates, three fresh passes each | 750 | 0 | $9.00 |
| Stage B: the frozen judge on 171 under three salts; v2 under two more | 513 | 342 | $7.87 |
| Stage B: about 33 probe bases and their variants (8 kinds each); v2 on the 4 mutant kinds | about 300 | about 132 | $4.26 |
| Stage C: the five reference days | 431 | 0 | $5.17 |
| **Before any label** | 3,494 | 474 | **$44 at $0.012; $39 to $41 at the measured cost; cap $60** |
| Only if stage C passes: re-judge days 6 to 11 | about 940 | 0 | about $11 |

Human time: about 15 minutes for the decisions, then the 60 blind labels, about 55 minutes.

## Risks, and how the design meets them

- **Every change is fitted to v5's tuning data.** The four answers and the risk case are named
  and leave the eligibility count; the build check and the kept rule keep them in view; stage C
  alone is evidence.
- **The judge assigns its own gap types.** A supported claim whose term a quote spells another way
  could be typed `term` and fail. The prompt's other-form note, the code downgrade for the
  service's own name, the kept rule (six supported answers that v2 passes, among them the
  acronym's plural), stage A's false-fail cap and stage B's no-new-strictness gate are the guards.
  The draft-then-revision order shows whether the rule helps or hurts.
- **The judge may type fewer gaps than its reasons name today.** Then the rule removes fewer false
  passes, and the skeptic's *s* in the 50s is the second line, resting on one 5-point gap.
- **The own-name map has gaps.** A missed name only sends the flag to the skeptic, as in v5.
- **Tight caps and a binding build check.** On the 93 the caps are 2.67 and 1.33 as in v5; on the
  32 the frozen version must beat 7.33 errors per pass. Three passes per version make the means
  steadier; the per-pass figures are reported.
- **Selection.** Fresh salts for the choice, and the simulation above (12% for a version just
  over the caps).
- **The fail-rate cap of stage C.** Reported per cell at every stage A step, before stage B is
  paid for.
- **Noise and cost.** Three passes everywhere in stage A, the pilot's stop at 1.3 times the
  estimate, a cumulative cap.

## Lessons from v5, applied

- A fix seen on one pass is a hypothesis: three passes per version, and a choice on fresh ones.
- The judge's scores are levels, and at the level where the labels split, its reasons separate
  them: the gap type makes the reason a field that a rule can read.
- Two calls had to agree for a hedge that names the service: code decides that case.
- A write-up must be checked against every record: every figure here is recomputed by a committed
  script from the round's records, and the readings that are not recomputations are marked as
  such.

## Decisions (to approve)

Recommended defaults are marked. Approval covers the `graders/` changes AGENTS.md requires
agreement for, up to three revisions in stage A, and the paid steps up to the cap.

1. **The design**: the process, the named list with the 93 and 32 counts, the gap types with the
   two rule-decided types and the code downgrade for the service's own name, the verdict-time
   clearing, the grid, the names count as reported only, and the draft-then-revision order.
   *Recommended.* Alternatives: gap types from the first draft (saves one version and about $4.50,
   loses the isolation); code only throughout; or giving the judge the cited services' names as
   one line of input, derived from the citation URLs, instead of the code downgrade (an input
   change, and a narrow URL channel).
2. **docs-mix-058**: (a) pre-registered as a known miss, exempt from the kept rule and reported.
   *Recommended.* (b) In the kept rule, which the design cannot pass without showing link targets.
   (c) Show console link targets as text naming the console (not recommended: one answer, 3,389
   such cells in the snapshot, and it reopens the URL channel).
3. **The rubric**: (a) rubric v5's text unchanged; the publisher note lives in the gap-type
   instruction and the service's own name in code. *Recommended.* (b) Add rubric v2.1's sentence
   on the publisher and the service's own name to the rubric, as rubric v6.
4. **Fresh labels at stage C**: (a) 60, about 55 minutes. *Recommended.* (b) 30, with v5's weaker
   power figures.
5. **Budget**: (a) a new ledger capped at $60 before any label, estimate about $44, the pilot's
   stop at 1.3 times $0.012 per answer. *Recommended.* (b) Carry v5's remaining $53.05 as the cap.
6. **Scope of a pass**: online only, as v5's decision 6. *Recommended.*

## Appendix: named cases (frozen 2026-10-29)

v5's 25: online-d01-085, online-d02-071, online-d03-068, online-d04-030, online-d04-032,
online-d04-072, docs-ai-001-p2, docs-ai-004-p3, docs-ai-007, docs-chg-004, docs-chg-007,
docs-chg-008, docs-chg-022, docs-core-025, docs-core-034, docs-int-014, docs-int-020,
docs-int-029, docs-mix-022, docs-mix-024, docs-mix-049, docs-mix-050, docs-mix-057, docs-mix-078,
docs-mix-090.

Added for v6: docs-ai-003-p3 (the hedge that names the service), docs-chg-010 (a name no quote
states, passed at the level), docs-mix-042 (a type name no quote states, scored across the
level), docs-mix-058 (support in a link target), and docs-ai-003-p1 (the acronym's plural, the
`term` rule's named risk).

On the tuning set the 30 cases are 32 answers (10 labeled supported, 7 of which v2 passes),
holding all 9 of v2's stored errors there; on the screen, 29 answers, as in v5.
