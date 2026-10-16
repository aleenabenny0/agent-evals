# ADR 0008: The gated improvement loop

**Status:** accepted (2026-10-16), first used for iteration 1 the same day.

## Context

Phase 5 of the plan and Brief 02's M6 ask for improvements that are measured, gated and
reversible, with a changelog that shows the holdout numbers moving over iterations. Three things
had to be true before the first iteration could run honestly: a frozen holdout nobody tunes on
(ADR 0006), a noise floor for comparisons (the flip rate, ADR 0007), and a hallucination metric
that is validated against a human (`judge_supported`, `docs/judge-validation.md`). All three
existed by 2026-10-16.

## Decision

- **One change per iteration.** A change is a prompt version, a tool description, a lesson text
  or a validator rule, never several at once, so the comparison attributes the effect to one cause.
- **Champion and challenger run from the same code.** Behaviour that an iteration changes is
  selected by config (`docs_agent.prompt_version`, keys of `SYSTEM_PROMPTS`), and the challenger
  is a config file beside `default.yaml`. The agent version fingerprint already covers the config
  and every prompt, so the two runs carry distinct versions. Rollback is one line in the config.
- **The gate, on the dev split with 3 attempts per case**, read from `evals compare` on the same
  cases: (1) the target metric's paired 95% interval excludes zero in the better direction;
  (2) no monitored metric (`correct`, `fabricated_citation`, `uncited_answer`,
  `wrong_abstention`, `missed_abstention`, `rejected_submissions`) has an interval that excludes
  zero in the worse direction; (3) no case type or service area drops by more than the champion's
  flip rate, which is the band inside which a difference is noise. The target of iteration 1 is
  `judge_supported`; later iterations may target `correct`, `wrong_abstention` or cost.
- **Then the test split once.** A challenger that passes the gate is run once on the frozen test
  split and judged once. The CHANGELOG entry records both splits with intervals, the run ids,
  the dataset version and the failure categories the change targeted. A challenger that fails
  the gate is also recorded, with its numbers, so the changelog shows the rejections too.
- **Promotion** is editing `prompt_version` in `default.yaml`. The previous champion's config
  stays in `configs/` for the next comparison.
- **The judge is part of the gate but not of the agent.** It is run after each dev run with
  `evals judge`, costs about $3 per 390 attempts at Sonnet prices, and its own validation does not
  change between iterations; if an iteration changes how answers look in a way that could change
  the judge's accuracy (for example far longer answers), a 30-answer spot check against hand
  labels is due before the number is trusted.

## Consequences

- Each iteration costs about $1 for the dev run, $3 for judging it, and $1.35 for the test run
  and its judging, plus the agent's own calls, all cached for exact re-runs.
- Test is touched once per shipped iteration, and never by a failed one, so its exposure grows
  slowly and is logged.
- The headline chart the briefs want is read straight from the CHANGELOG: holdout `correct`,
  `judge_supported` and cost per success, one point per iteration.
