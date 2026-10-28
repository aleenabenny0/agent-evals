# Failure taxonomy: first dev run

**Summary.** This taxonomy reads every failure in the docs agent's first dev run
(`docs-20261011-223111-702e87`): 32 failing attempts out of 390, each read in full and given one
cause. None of the 32 contains a factually wrong claim: 13 are keyword-grader false negatives, 12
are hedged abstentions (the right answer, marked `abstained: true`), and the other 6 come from two
cases, one unanswerable case answered as a false premise and one case defect. Baselines show that
retrieval is worth about 37 points of correctness on this suite, and that handing the agent the
gold pages adds no detectable gain. A later section, from the iteration 3 error analysis
(2026-10-17), classifies the 157 unsupported claims in the 82 v3 champion answers that the
validated judge fails on fully supported, and finds that the agent mostly had the evidence and
did not quote it. On the first run the judge, not yet validated against hand labels, passed only
0.177 of cited answers as supported, and 82% of answers carried at least one claim drawn from
pages the agent read but did not quote. Both analyses cover the dev split only; the test split's
three failures were not read.

**Run:** `docs-20261011-223111-702e87` (docs agent `fbfc5355d106`, dev split, 130 cases, 3
attempts, 390 attempts). **Result:** correct 0.918 (95% CI 0.874 to 0.956); 32 failing attempts.
Every one of the 32 was read in full with `evals trace` (the compact dump is reproducible from the
store). One cause per failure. Dev split only; the test split's three failures were not read.

The headline finding: **none of the 32 contains a factually wrong claim.** The agent's errors in
this run are errors of protocol (saying "abstained" while answering) and of the instrument
(keyword matching too narrow, three case defects). That changes what to fix first.

| Category | Attempts | Cases | Share |
|---|---|---|---|
| A. Keyword grader false negative | 13 | 9 | 41% |
| B. Hedged abstention: right answer, `abstained: true` | 12 | 6 | 38% |
| C. Unanswerable answered as a false premise | 3 | 1 | 9% |
| D. Case defect (gold demands an unasked fact) | 3 | 1 | 9% |
| E. Fabrication, wrong fact, wrong page cited | 0 | 0 | 0% |
| Side finding: guessed URLs outside the snapshot (`tool_errors`) | 0.38 per attempt | | |

Two traces per category below. Reproduce any of them with
`uv run evals trace docs-20261011-223111-702e87 <case> --attempt <n>`.

## A. Keyword grader false negative (13 attempts, 9 cases)

The answer is correct and the judge should pass it; `must_include` alternatives did not contain
the agent's phrasing.

- `docs-ai-002` and its three paraphrases, 6 attempts: gold alternatives are "no additional",
  "no extra", "not charged", "no routing cost". The agent wrote "does not charge an additional
  routing fee", "doesn't charge a separate routing fee", "doesn't charge a routing surcharge".
  All cite the page sentence "There's no additional routing cost".
- `docs-core-018` #3: alternatives "auto-upgraded", "automatically upgraded"; the agent wrote
  "Amazon EKS then automatically upgrades any control plane". Morphology, not meaning.
- Also `docs-mix-079` #3 ("has no charge" vs "free"), `docs-core-011` #3 ("name is fixed at
  creation" vs "cannot rename"), `docs-core-034` #3 ("recommends against" vs "do not"),
  `docs-int-020` #2 and #3 ("no additional charge", "$0" vs "free"), `docs-ai-001` #1 and
  `docs-ai-001-p2` #2 ("do not currently support" vs "do not support"), `docs-chg-016` #2 (listed
  the sixteen Regions instead of the "May 20, 2025" cut-off; same fact, other side of the table).

Proposed fix: this is exactly what the judge's "responsive" verdict is for. The judge pass over
this run (judge `b23a6c3aef09`, same evening) marked **21 of the 32 failing attempts as
responsive**: all 13 in this category, the 3 in category C, the 3 in category D, and the two
`docs-ai-001` attempts that answered without abstaining. The 11 it did not mark are the hedged
abstentions, which the judge skips by design. Once the judge is validated against the author's
labels, a dev answer with `correct = 0` and `judge_responsive = 1` becomes a suspected grader
miss, to be confirmed by the author; confirmed ones get wider alternatives. The test split is
never touched this way (its false negatives stay, and are reported as such).

## B. Hedged abstention (12 attempts, 6 cases)

The agent states the right answer in `answer` and sets `abstained: true` anyway, usually with
"treat this as an inference" or "I could not open the detailed page". `AnswerGrader` scores an
abstention on an answerable case as wrong, which is the right rule: a user reading "abstained"
does not get an answer.

- `docs-ai-001` group, 5 attempts: "So the answer is no, as far as these docs show", `abstained:
  true`. Root cause one step earlier: search ranked `cross-region-inference.html`, the page with
  the explicit sentence, first for every query, and the agent read `inference-profiles.html` and
  `prov-throughput.html` instead, then reasoned from their silence. Only the attempt that read
  the top hit (`docs-ai-001-p2` #2) answered with confidence.
- `docs-int-017`, 3 attempts: rejects the premise exactly as the gold does ("managed rotation
  doesn't use a Lambda function"), then abstains because the second half of the question, where
  the template lives, is on a page outside the snapshot. A compound question with an
  unanswerable half; see the case note under C below.
- Also `docs-chg-007` #1 ("Probably not. Don't plan a new platform around Model Monitor...",
  abstained), `docs-mix-050` #1 and #3 (the overview page only names the "Scaling to Zero ACUs"
  section; the detail page is not in the snapshot).

Proposed fix: a prompt rule in the next gated iteration (phase 5): "If the documents let you
answer, answer; `abstained` is for when they do not. Put your uncertainty in `confidence`, not in
`abstained`." Plus a retrieval rule: read the top search hit before reasoning from other pages.
Both are measured, not assumed: champion versus challenger on dev, then test, with CHANGELOG.

## C. Unanswerable answered as a false premise (3 attempts, 1 case)

`docs-int-023` asks on what date Step Functions will raise the 1 MB definition limit. Gold:
abstain, nothing is announced. The agent, three times: "There is no date... the limit is a hard
quota... the premise is not supported", `abstained: false`, citing the quota row. The content is
right and the format is the opposite of category B. The question reads like a false premise, so
the agent treated it as one. Case note: either reclassify the case as `false_premise` with
"no date" alternatives, or keep it unanswerable and let the prompt rule above settle the format.

## D. Case defect (3 attempts, 1 case)

`docs-chg-025` asks only whether PITR is fixed at 35 days; the gold also requires the clause
"changing the window does not change the price", which the question does not ask. The agent
answered the question asked, correctly, three times. Drafting error (in the LLM-drafted case,
2026-10-11), caught by the first run. The fix is the author's call: drop that `must_include` group
or extend the question. It is a case fix, not a gold-to-pass edit, and the decision is still
open.

## What the judge adds on the passing attempts

`judge_responsive` passed 0.997 of judged answers; the one failure it found among code passes is
`docs-core-028` #3, where the agent confirmed the DynamoDB half and hedged the S3 Block Public
Access half ("check it yourself") that the reference states outright. A real catch that keywords
cannot make.

`judge_supported` passed only **0.177** of cited answers. Reading the reasons: the core claim is
almost always supported; what fails is the agent's added context, drawn from pages it read but
did not quote ("inference profiles are used to route requests across Regions", "the 10% saving
belongs to the Global profile", when the cited table row has no headers). By the brief's
definition that is a claim-level unsupported rate, and 82% of answers carry at least one such
claim. Whether a human reader agrees that this should fail an answer is exactly what the
judge-test kappa will tell; if the author's labels pass those answers, the judge prompt is too
strict and is revised on judge-dev. Either way the agent-side fix is the same and belongs to
phase 5: cite what you state, or do not state it.

## E. Fabrication and wrong facts: none observed

`fabricated_citation` was 0.021 and `unsupported_entity` 0.046 in the run, so the metrics did fire
on some *passing* attempts; those were not failures of `correct` and are the judge's and the
labels' job to characterise. The two `unsupported_entity` reasons visible among the failures were
list numerals ("1.", "2.", "3.") in a numbered answer, a grounding-grader false positive fixed the
same day (grounding grader v2 ignores list markers).

## Side finding: guessed URLs (tool_errors 0.38 per attempt)

Every `tool_errors` increment in the failing attempts is `read_page` on a URL the agent inferred
from a link in a page it had read (`prov-thru-use.html`, `rotate-secrets_lambda.html`,
`aurora-serverless-v2-auto-pause.html`), none of which are in the snapshot. Harmless to
correctness, costly in calls (the agent then searches again), and it feeds category B: the
missing detail page becomes the reason to hedge. Candidate fix for phase 5: the `read_page`
error text names the pages that *are* in the snapshot for that guide, or the tool description
says only URLs returned by `search_docs` exist.

## What the baselines add

- Closed-book (same model, no tools): correct 0.546 (0.460 to 0.629). Paired difference against
  the docs agent -0.372 (95% CI -0.462 to -0.282), McNemar p = 3e-12. Retrieval is worth about 37
  points on this suite; the model's memory alone is wrong or silent on nearly half the questions.
- Oracle (gold pages handed over, no search): correct 0.923 (0.864 to 0.958), difference +0.005
  (95% CI -0.046 to +0.054), no detectable change. The agent's retrieval is already as good as
  being given the pages; what remains is categories A to D, not finding the page.

## Test split, for the record

`docs-20261011-225715-b67388`, 150 cases, 1 attempt: correct 0.987 (0.953 to 0.996),
fabricated_citation 0.028, unsupported_entity 0.060, $0.35. Three failures, unread. The test
score is higher than dev's because dev deliberately holds the paraphrase groups (whose source,
`docs-ai-001`, is category B six times over) and the two held cases; dev is built to be the
harder, more informative split.

## Unsupported claims in the v3 champion's answers (iteration 3 error analysis, 2026-10-17)

**Run:** `docs-20261016-214710-02abb7` (prompt v3, dev split). **Set:** all 82 attempt-1 answers
that the validated judge (`2cf75fa03e09`) fails on fully supported, of 115 answered; only 5 of
them also fail core supported. **Method:** eight reader agents of a type that inherits no session
instructions, with read-only tools, each took 10 or 11 answers. Each reader listed every
unsupported claim under the judge's rubric, not only the one the judge named, and gave it the
first cause that applies in the order below. "On a page the agent read" was decided by searching
the exported text of every page the agent read or cited, never from memory. The author's notes
went in as evidence for the 55 answers that had one. A ninth reader re-read 15 answers on its
own. Dev only. Data, per answer and per claim: `results/iteration-3/error-analysis.json`; export
`scripts/export_unsupported_claims.py`; workflow `results/iteration-3/error-analysis-workflow.js`.

| Cause (first that applies) | Claims | Core | Answers with it | Answers where it is the only cause |
|---|---|---|---|---|
| About the docs: what they say, leave out, or where | 27 | 0 | 21 | 6 |
| Headerless row: a table value's column not quoted | 16 | 4 | 13 | 6 |
| On a page the agent read, not quoted | 82 | 7 | 57 | 31 |
| Beyond the quote: stronger, broader, or inferred | 27 | 5 | 25 | 4 |
| On no page the agent read | 4 | 0 | 4 | 0 |
| Other (a wrong unit conversion) | 1 | 0 | 1 | 1 |
| **Total** | **157** | **16** | **82** | |

- **The agent mostly had the evidence and did not quote it.** The commonest form is a quote cut
  short of the words the answer uses: docs-ai-005-p2's quote stops at "to identify", so nothing
  quoted says what the field identifies; docs-core-002's stops at "and are", dropping "subject to
  change"; docs-chg-016 quotes the Adjustable header but not the row's "Yes". v3's own rule
  ("a short quote ... one sentence or less") pushes in that direction.
- **Background knowledge is rare:** four claims on no page the agent read, all workarounds or
  asides (docs-core-011's create-copy-delete bucket rename, docs-int-007's dead-letter pattern).
- **Going beyond the quote** is mostly a strengthened modal or scope: "you must rotate manually"
  where the page says "can" (docs-core-037), "only needed for" where the quote lists one case
  (docs-int-029).
- **Statements about the docs** are the agent narrating its sources ("the Kendra page still
  mentions a free trial", "the docs do not say whether the 300-VPC limit can be increased").
- **Almost all of it is removable:** in 71 of 82 answers the answer stays complete with every
  unsupported claim deleted (5 partly, 6 no), and in 72 the claims could have been kept by quoting
  text the agent had read (47 fully, 25 partly).
- **The judge's verdict held up:** the readers confirmed at least one unsupported claim in 78 of
  82 and were unsure in 4; none found an answer that was in fact fully supported.

**Reliability.** The independent second reading gave the identical set of causes on 11 of 15
answers (mean Jaccard 0.80), the same verdict on whether the unsupported content was removable
on 15 of 15, and the same verdict on the judge on 13 of 15. All four differences are claims about
the docs that one reader counted and the other let pass, so that row is the least certain; the
ranking of causes does not depend on it.

Fixing a cause flips an answer only if every unsupported claim in it is fixed: quoting what was
on the page alone would make 31 answers fully supported; together with going beyond the quote, 45;
together with that and the statements about the docs, 65.

