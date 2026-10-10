# ADR 0001: Content-addressed call cache with per-attempt salt

**Status:** accepted

## Context

Eval runs call the model hundreds of times. Re-running a suite to fix a grader, add a metric or
reproduce a result should not cost money or return different answers. But measuring
reliability (pass^k) requires several *independent* attempts at the same case.

## Decision

Every model request is keyed by `sha256(canonical_json(request) + salt)` and its response is
stored as a JSON file under `.cache/llm/`. The salt is `"<case_id>#<attempt>"`.

- Same case, same attempt number, same request: cache hit, identical response, zero cost.
- Same case, different attempt number: different key, so a fresh sample from the model.
- Any change to the prompt, tools, model or settings changes the request, so the cache can
  never serve a stale answer for a changed agent.

## Consequences

- `evals run` on an unchanged agent is free and deterministic, so results are reproducible.
- Graders can be changed and re-run over cached trajectories without new API calls.
- Reports track `cached_calls` separately; the budget cap only counts uncached spend.
- Deleting `.cache/` forces fresh samples.
