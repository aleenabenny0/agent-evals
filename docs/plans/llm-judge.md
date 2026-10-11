# Proposal: the LLM judge (phase 2 step 6)

**Status:** proposed, 2026-10-11. Touches `graders/`, so it needs Aleena's agreement before code.
Running it costs money (Sonnet 5.5 calls), so the validation run is also hers to approve.

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
  back through a strict `submit_verdict` tool with `tool_choice` forcing it, so parsing is
  schema-checked; a missing or malformed verdict is a `grader_error`, never a silent pass.
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

1. **Hand labels.** Aleena labels 100 to 200 dev-split answers (supported? responsive?) with
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

## Decisions (Aleena, 2026-10-11)

- **Two verdicts**, supported and responsive. No separate "complete": an incomplete answer to a
  multi-step question is not responsive, so it fails that verdict.
- **Hand-labeling in `evals review` style**: a terminal walk through stored answers, one at a
  time, with the question, the answer, its citations in page context, and two yes/no prompts.
- **Judge model: claude-sonnet-5-5.**

The plan is approved with these answers. The code shipped the same day (see Mechanics); what
remains is the validation run: labels, `evals judge`, `evals judge-validate`, mutants, probes.
