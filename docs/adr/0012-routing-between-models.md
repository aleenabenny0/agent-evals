# ADR 0012: Routing between models, fitted on dev and judged once on test

**Status:** accepted (2026-11-03).

## Context

Brief 03 asks whether choosing a model per question gives the same quality for less. The docs agent
runs on Haiku 5.5. Sonnet 5.5 costs 20 times as much per token and Opus 5.5 40 times. On the frozen
test split Haiku answers 147 of 150 correctly, so `correct` leaves almost nothing to route for, but
only 31.8% of its answers are fully supported by their quotes (`docs/plans/phase-9-routing.md`).

## Decision

- **The target is a good answer:** correct, and either an abstention or fully supported by the
  validated judge. `correct` alone is analyzed beside it.
- **One attempt per model per question.** Every model is compared on attempt 1, so the matrix is
  paired and no model gets more tries.
- **A router sees only what a live service could.** `src/agent_evals/routing.py` gives a router the
  question, and for a cascade, the first answer's escalation signal: it abstained, errored, broke the
  output contract or cited nothing. Only the oracle reads outcomes, and it is reported as an upper
  bound, not a router.
- **Routers:** single models, a per-type choice (for comparison only, since a live question has no
  type label), a Haiku-to-Sonnet cascade, and nearest neighbours over TF-IDF vectors of the
  question, with k and its tolerance chosen by leave-one-out on dev.
- **Costs at list price with no prompt cache,** from each answer's tokens, because Haiku's runs
  predate the cache and billed costs would favor the new models.
- **One router per target is chosen on dev and frozen,** with a hash, before the test runs. Test is
  read once. The claim is the chosen router against always-best, paired over the test questions,
  with a non-inferiority margin of 5 points, and against always-Haiku, with an interval that must
  exclude zero.
- **No new dependency.** TF-IDF and the neighbour search are a few dozen lines of Python, which
  keeps every step readable and avoids scikit-learn for 280 questions.

Two alternatives were dropped: embeddings from a hosted model, which add a second paid provider and
a network call per route; and a learned classifier, which has too few failures to learn from on
130 dev questions.

## Consequences

- With 150 test questions, small differences cannot be detected. The smallest detectable
  difference is computed from dev before the test look and reported with the result.
- The prompt was tuned on Haiku over four gated iterations, which may favor Haiku.
- If the chosen router is usable, the service can serve it as `model: auto` without changing the
  agent.
