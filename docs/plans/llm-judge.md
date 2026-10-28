# Proposal: the LLM judge (phase 2 step 6)

**Summary.** This proposal defines the LLM judge: the two questions it answers that code cannot
settle (is every factual claim backed by the cited quotes, and does the answer address the question
as asked), how it runs, how it is validated before any of its numbers count, and what it costs.
The plan was approved on 2026-10-11 with three decisions (two verdicts, terminal-based hand
labeling, claude-sonnet-5-5 as the judge model), and the code shipped the same day. A first
validation against 114 hand labels gave kappa 0.26 on "supported", which led to splitting that
verdict into "core supported" and "fully supported" under one written rubric shared by the judge
and the labeler. Two later rubric decisions from a blind adjudication apply from judge v4 on, and
v2 stays the validated judge until v4 is scored against fresh labels under a rule fixed in advance.
Validation results go in `docs/judge-validation.md`.

**Status:** proposed, 2026-10-11. Touches `graders/`, so it needed approval before code.
Running it costs money (Sonnet 5.5 calls), so the validation run also needed approval.

## What the judge decides, and what it does not

Code already settles: did the answer contain the required phrases, did the agent abstain when it
should, do the citations exist and quote the page verbatim, was the output well formed. The judge
is for the two questions code cannot settle:

1. **Supported** - is every factual claim in the answer backed by the cited quotes? (Catches a
   correct-sounding answer whose citations do not actually say that.)
2. **Responsive** - does the answer actually answer the question as asked, including rejecting a
   false premise, rather than answering a nearby question? (Catches the "right words, wrong
   recommendation" failure that put two pilot cases on hold.)

Each is a verdict in {pass, fail} with a one-sentence reason. The judge never sees the gold
answer for the "supported" question (it judges answer against citations only) and sees it for the
"responsive" question. It never changes a grade that code already decided.

## Mechanics (implemented 2026-10-11)

- `graders/judge.py`: `JudgeGrader` uses `LLMClient` like the agent, so every call is cached,
  timed and counted. Model and prompts come from `judge:` in the config (`claude-sonnet-5-5`,
  `JUDGE_SUPPORTED_V1`, `JUDGE_RESPONSIVE_V1` in `graders/judge_prompts.py`). The verdict comes
  back through a strict `submit_verdict` tool the prompt asks for, so parsing is schema-checked;
  a missing or malformed verdict is a `grader_error`, never a silent pass. (A forced
  `tool_choice` was tried first; claude-sonnet-5-5 rejects it with a 400, so the first judge run
  produced 346 grader errors and zero spend before the fix.)
  Metrics: `judge_supported` and `judge_responsive`, each 0/1 with the judge's reason.
- The judge runs **after** a run, never inside `evals run`: `evals judge RUN_ID` reads the stored
  answers, judges the answered, non-abstained ones (and asks "supported" only when there are
  citations), writes the grades next to the code graders' grades and records the judge version,
  model and salt in the run manifest. `evals report` then shows the two metrics with intervals
  like any other. `--replace` re-judges; `--salt probe` re-samples with a fresh cache key for the
  stability check, since identical requests otherwise replay from the cache.
- `evals label RUN_ID --labeler NAME` is the hand-labeling walk: question, answer, each citation
  highlighted in its page, then "supported?"; then the gold answer, then "responsive?". Labels go
  in a `labels` table in the results store, each tagged judge-dev or judge-test by a hash of the
  case id (`judge_validation.label_split`), so the split cannot be nudged and every attempt of a
  case lands on one side.
- `evals judge-validate RUN_ID --split judge-test` joins labels with verdicts and prints, per
  verdict, Cohen's kappa (`stats.core.cohens_kappa`), TPR and TNR with Wilson intervals, and the
  confusion counts. Positive class = a real failure: TPR is the share of human-failed answers the
  judge also fails; TNR the share of human-passed answers it also passes.

## Validation before any judge number counts (from the roadmap)

1. **Hand labels.** The author labels 100 to 200 dev-split answers (supported? responsive?) with
   `evals review`-style tooling extended for answers. The labels are split into **judge-dev**
   (for tuning the judge prompt) and **judge-test** (for reporting only; never looked at while
   tuning). On judge-test, report Cohen's kappa (at least 0.7 on each question), **TPR** (the
   judge catches real failures) and **TNR** (it does not flag good answers), with n.
2. **Mutation tests.** Take correct, supported answers and plant one error each (wrong number,
   wrong service, dropped qualifier, citation swapped to an unrelated quote). The judge must flag
   at least 90% of mutants as failing.
3. **Bias probes.** Same content, longer vs shorter; same pair, swapped order. Verdicts must not
   move.
4. **Stability.** Re-judge the same set; agreement with itself must be near total.

Results go in `docs/judge-validation.md` (the brief's artifact) and an ADR: kappa, TPR, TNR,
the confusion matrix, mutation catch rate and probe outcomes, each with n. If a bar is missed,
the prompt changes and validation repeats; the judge does not ship on a near miss.

## Cost

Judging one answer is one Sonnet 5.5 call with the answer, citations and question: roughly 1.5K
input tokens and 200 output tokens, under a cent per answer at first-party rates. Validating on
100 labeled answers plus mutants and probes is a few hundred calls.

## Decisions (2026-10-11)

- **Two verdicts**, supported and responsive. No separate "complete": an incomplete answer to a
  multi-step question is not responsive, so it fails that verdict.
- **Hand-labeling in `evals review` style**: a terminal walk through stored answers, one at a
  time, with the question, the answer, its citations in page context, and two yes/no prompts.
- **Judge model: claude-sonnet-5-5.**

The plan is approved with these answers. The code shipped the same day (see Mechanics); what
remains is the validation run: labels, `evals judge`, `evals judge-validate`, mutants, probes.

## Revision (2026-10-12): two levels of "supported"

The first validation against 114 hand labels gave kappa 0.26 on "supported" (see
`docs/judge-validation.md`). Every disagreement was an answer whose core fact was quoted and whose
added context was not; the single every-claim verdict failed those, the labels passed about half
of them. Decision: the support question has two levels, judged in one call and labeled as two
questions, with one written rubric (`SUPPORT_RUBRIC` in `graders/judge_prompts.py`) shown to the
judge and to the labeler alike:

- **core supported** (`judge_core_supported`): the claims that answer the question are in the
  quotes. This is the response-level hallucination metric for reports.
- **fully supported** (`judge_supported`): every claim, context included, is in the quotes. This
  is the brief's claim-level unsupported rate. It can only pass when core passes.

Rubric rules settled at the same time: a table cell quoted without its header supports only what
the cell literally says ("Yes" is not "adjustable"); advice, hedges and offers to help are not
claims; support, not truth, is judged. Confirmed 2026-10-16 after the first judge-dev round showed
it to be the main source of core disagreements: the rule stays strict for rows as well as cells,
and the labels apply it the same way the judge does. Refinement the same day: a header fragment
quoted alongside the value (for example "Deprecation date") is the one thing that establishes a
column's meaning; prose near the row ("the deprecated runtime") does not. This changed the judge's
prompt, so the judge version moved and the dev run was re-judged before the final scoring. Validation restarts: relabel judge-dev under the new
rubric, tune the judge against it, relabel judge-test once and score once. "Responsive" gets its
first real negatives from 30 to 40 closed-book answers, labeled with that question only.

## Revision (2026-10-16): two rubric questions from the v2-vs-v3 adjudication

The blind adjudication of the 40-label comparison (`docs/judge-validation.md` section 7) left two
disagreements that belonged to the rubric rather than to anyone's mistake. Decisions:

1. **"The docs don't say X" is a hedge** and needs no support. Written into draft rubric v4
   (`SUPPORT_RUBRICS["v4"]` in `graders/judge_prompts.py`, `configs/judge-v4.yaml`).
2. **Price factors are not part of the main answer** when a question asks how something is
   billed; they are peripheral. Wording drafted, then approved on 2026-10-17: "When a question
   asks how something is billed, the core claims are how and when you are charged (for example,
   billed hourly until you delete it). The factors that set the price, such as the model, the
   number of units bought or the length of a commitment, are peripheral."

Both apply from judge v4 on (`bf1a8e9f376a`). v2 stays the validated judge until v4
is scored against fresh labels under a rule fixed in advance; if v4 is adopted, every run in the
changelog is re-judged with it so the history stays on one judge version.
