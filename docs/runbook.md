# Runbook: the docs agent service

For whoever is on call. Each alert in `deploy/alerts.yml` has a section here: what it means, how to
check it, and what to do. **Status:** written from the design. The failover and canary drills
(`docs/plans/phase-8-llm-ops.md`) will correct it with what really happened.

## First steps, for any alert

1. Open the dashboard at http://127.0.0.1:3000 and look at the last 30 minutes.
2. Check the service: `curl http://127.0.0.1:8000/readyz` names which check is failing.
3. See what is live: `uv run evals registry show`.

To find one request, take its `request_id` from the response or the logs and search for it in
Jaeger at http://127.0.0.1:16686.

## The one action that fixes most things

If a change caused the problem, roll it back. This rewrites a local file, needs no deploy and no
restart, and works with the providers down.

```
uv run evals registry rollback --why "what you saw" --who "your name"
```

With a canary live, this drops the canary. With none, it goes back to the previous champion. Check
the result with `uv run evals registry show`, and the log in `results/registry-log.jsonl`.

## Capacity

Measured on the fake provider at about 5 seconds an answer (`docs/plans/phase-8-llm-ops.md`, part 7).

- Capacity is `max_concurrent` divided by the time an answer takes: about 1.5 a second at 8 slots.
- Refusals begin well before capacity, since arrivals bunch up: about 16% at 1.2 a second on 8 slots.
  To refuse under 1% at 1 a second takes 12 slots.
- One process tops out near 80 answers a second, and past that it fails with connection errors
  rather than refusals. Real answers are slow enough that slots run out first.

## ServiceDown

**Means:** Prometheus cannot reach the service.
**Check:** `docker compose -f deploy/docker-compose.yml ps` and `logs service`.
**Do:** restart it with `docker compose -f deploy/docker-compose.yml restart service`. If it dies at
startup, the usual causes are a missing snapshot in `data/snapshots/` or a bad registry file, since
at startup there is no last good state to fall back on.

## AnswersFailing

**Means:** more than 5% of questions get no answer. The dashboard's outcome panel says which kind.
- `provider_error`: every provider failed. See ProviderCircuitOpen.
- `agent_failed`: the provider answered but the agent produced no answer. This points at the
  prompt, so check whether a canary or a promotion started it, and roll back if so.
- `timeout`: see AnswersSlow.

## AnswersSlow

**Means:** the 95th percentile answer takes over a minute.
**Check:** a slow trace in Jaeger shows whether the time is in model calls or in many steps. Many
steps suggests a prompt problem, and long model calls suggest the provider.
**Do:** if it is the provider, check its status page and expect the circuit to open. If a canary
started it, roll back.

## ProviderCircuitOpen

**Means:** a provider failed repeatedly, so calls skip it for a cool-down, then one call tests it.
Answers continue on the fallback when one is configured.
**Check:** `gateway_provider_failures_total` by `kind`, and the provider's status page. An
authentication error means a key is wrong or expired, and needs a person.
**Do:** nothing, if the fallback is carrying traffic: the circuit closes by itself once the provider
recovers. Answers on the fallback come from a different model, which was never gated, so say so if
the outage is long.
**With no fallback configured** this means an outage: questions fail until the provider recovers.

## AtCapacity

**Means:** the service is refusing questions with a 429 because every slot is busy. Refusing at once
is deliberate: queueing in front of a slow backend makes an overload worse.
**Check:** whether traffic rose or answers slowed. A slower provider fills the slots with no rise in
traffic.
**Do:** if traffic rose, raise `service.max_concurrent` within what the provider's rate limit allows.
If answers slowed, fix that first, because more slots only stack up more waiting work.

## SpendHigh

**Means:** over half the daily cap went within an hour. Past the cap the service refuses questions
with a 503 until the next UTC day.
**Check:** spend per prompt version on the dashboard. A prompt that takes many more steps shows here
first.
**Do:** if a change caused it, roll back. Otherwise decide whether the traffic is worth the money
before raising `service.daily_budget_usd`.

## RegistryFileBad

**Means:** `configs/registry.yaml` cannot be used, so the service is running on the last good state
and **a rollback would not take effect**.
**Check:** `uv run evals registry show` says what is wrong: bad YAML, or a prompt text that no longer
matches its recorded hash.
**Do:** `git checkout configs/registry.yaml`, then set the pointers again with `evals registry`. If a
prompt's text was edited in place, that is the bug: a change to a prompt is a new version.
