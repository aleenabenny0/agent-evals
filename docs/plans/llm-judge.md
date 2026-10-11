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

## Mechanics

- `graders/judge.py`: `JudgeGrader` using `LLMClient` like the agent, so every call is cached,
  timed and counted. Model `claude-sonnet-5-5`, prompt versioned (`JUDGE_PROMPT_V1`), verdict
  returned through a strict `submit_verdict` tool so parsing is schema-checked. Metrics:
  `judge_supported`, `judge_responsive`, each 0/1 with the reason.
- Runs only on cases where code could not settle correctness, or on all cases when `--judge all`
  is passed, so the normal path stays cheap.
- The judge's model id and prompt hash go into the run manifest and the grader version.

## Validation before any judge number counts (from the roadmap)

1. **Hand labels.** Aleena labels about 100 dev-split answers (supported? responsive?) with
   `evals review`-style tooling extended for answers. Cohen's kappa between judge and labels must
   be at least 0.7 on each question.
2. **Mutation tests.** Take correct, supported answers and plant one error each (wrong number,
   wrong service, dropped qualifier, citation swapped to an unrelated quote). The judge must flag
   at least 90% of mutants as failing.
3. **Bias probes.** Same content, longer vs shorter; same pair, swapped order. Verdicts must not
   move.
4. **Stability.** Re-judge the same set; agreement with itself must be near total.

Results go in an ADR with the kappa, mutation catch rate and probe outcomes. If a bar is missed,
the prompt changes and validation repeats; the judge does not ship on a near miss.

## Cost

Judging one answer is one Sonnet 5.5 call with the answer, citations and question: roughly 1.5K
input tokens and 200 output tokens, under a cent per answer at first-party rates. Validating on
100 labeled answers plus mutants and probes is a few hundred calls.

## Open questions for Aleena

- Agree the two verdicts above, or add a third (for example, "complete": did it answer every part
  of a multi-step question)?
- Hand-labeling 100 answers is about two hours. Do it in `evals review` style, or in a
  spreadsheet exported from the results store?
- Sonnet 5.5 as planned, or Opus 5.5 for the judge and accept the higher cost?
