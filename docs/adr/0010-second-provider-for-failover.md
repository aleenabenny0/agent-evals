# ADR 0010: A second model provider, for failover

**Status:** accepted (2026-11-02), with decision 1 of `docs/plans/phase-8-llm-ops.md`. The fallback
is OpenAI's `gpt-5-mini`, set in `configs/service.yaml`. The code is built and tested offline, and it
does nothing until the provider's key is set. As of 2026-11-02 no key is set and the project has no
budget left for paid API calls, so the real provider has never been called.

## Context

On 2026-10-17 the project chose to keep one provider behind `LLMClient` until the routing phase,
when several models would be needed anyway (`docs/plans/brief-alignment.md`). The LLM Ops phase comes
first and asks for real failover, which cannot be shown with one provider.

Failover also has a timing problem that a retry does not. The docs agent's answer is a loop of up
to twelve model calls, and a provider can go down between any two of them. Restarting the answer on
the second provider would throw away the work and double the cost, at the moment a provider is
already failing.

## Decision

- **`ResilientClient` is an `LLMClient`** that wraps others in order of preference, so the agent,
  the harness and the graders do not change (`harness/gateway.py`).
- **Failover is per model call, not per answer.** An answer in progress continues on the next
  provider.
- **Requests keep the shape the agent already uses.** `OpenAICompatibleClient` translates each call
  both ways, the whole conversation included, which is what per-call failover needs
  (`harness/openai_compat.py`). It speaks the OpenAI-compatible API, so the provider is a base URL
  and a model name in the config, not a code path.
- **A circuit breaker per provider,** so an outage costs a few timeouts rather than one per request.
- **A provider's failure and a bad request are different.** A request the provider refuses as
  malformed is raised at once and trips nothing, since the next provider would refuse it too.
- **The cost of an answer is priced per call,** at the prices of whoever served that call.

## Consequences

- Every answer can name who served it, so a failover is visible in the response, the logs and the
  metrics.
- Strict tool schemas are not sent to the second provider, and thinking blocks are dropped on
  translation. The agent validates every tool input itself either way.
- The second provider is a different model, so answers during an outage are not the answers that
  were gated. How much worse they are is measured on the dev suite and stated in the runbook, and
  nothing is ever promoted on that path: such a run records its provider in its manifest, and
  `evals gate` refuses it, since the agent's version alone cannot tell the two runs apart.
- It costs more per token than the primary: $0.25 and $2.00 per million input and output tokens,
  against $0.10 and $0.50 (both read 2026-11-02). That is a price paid only during an outage.
- Mapping one conversation between two APIs is code that has to stay correct as they change. A
  gateway library would hide it, at the cost of a dependency in the one path every call takes; the
  translation is about a hundred lines and has its own tests.
- The routing phase can reuse this client rather than add one.
