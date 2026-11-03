# Runbook: the docs agent service

For whoever is on call. Each alert in `deploy/alerts.yml` has a section here: what it means, how to
check it, and what to do. **Status:** corrected by the canary drills of 2026-11-02
(`docs/postmortems/2026-11-02-release-drills.md`). The failover drill has not run, because the
project has no budget left for paid API calls, so the sections on failover still describe the
design.

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
the result with `uv run evals registry show`, and the log in `results/service/registry-log.jsonl`.

## Releasing a prompt version

1. Gate it first (`evals gate`, ADR 0008). The canary rule cannot see a prompt that gives confident,
   cited, wrong answers, so the gate is the only check on accuracy.
2. Start the canary: `uv run evals registry canary VERSION --share 0.2 --why "..." --who "..."`.
3. Watch the "Bad answers while a canary is live" panel. The rule looks at 20, 40 and 60 canary
   answers, and rolls the canary back by itself if it is worse than the champion.
4. Past the third look the rule has no more to say, and the canary stays at its share until a person
   acts. It has shown only the absence of a large fault. Promote it with
   `uv run evals registry promote --why "..." --who "..."`, or roll it back.

A service restart starts the looks again, so avoid one during a canary.

**What the rule can and cannot do.** A bad answer, to the rule, is one that errored, abstained,
cited nothing or read no page. It catches a broken prompt: a stale notice that made every answer
abstain was removed in about 5 minutes, after 20 bad answers, at 0.45 questions a second, and a
prompt that stopped the agent reading pages was removed in about 3 minutes (drill 1c). It does not
catch a prompt that reads pages and answers worse. A quiet canary is not evidence of accuracy.

## Service objectives

Set on 2026-11-02 from the real model: 733 champion answers in the drills and a short load test, on
simulated questions, on one machine. They are a first measurement, so treat them as starting points.

| Objective | Target | Measured |
|---|---|---|
| Accepted questions that get an answer | at least 99% | 1,005 of 1,008 |
| 95th percentile answer time | at most 25 s | 19.8 s |
| Questions refused, at 0.2 a second on 8 slots | under 1% | at most 0.4% predicted, see below |
| Cost per answer | at most $0.003 | $0.0019 |

The champion's bad-answer share, as the canary rule counts it, was 13%. Most of that is correct:
abstaining on questions the docs cannot answer is what the agent should do.

## Capacity

Measured on the real model on 8 slots (`docs/plans/phase-8-llm-ops.md`, part 7).

- An answer takes about 11 s at the median and about 5 model calls.
- Capacity is `max_concurrent` divided by the time an answer takes: about 0.6 a second on 8 slots.
- Refusals begin well before capacity, since arrivals bunch up. The Erlang B formula predicts them:
  it gave 8% to 11% at 0.45 a second, and three drills refused 8.1%, 8.9% and 9.7%. It gives at most
  0.4% at 0.2 a second, and under 1% at 0.45 a second takes 12 slots.
- The real limit on slots is the provider's rate limit, which these runs did not reach.
- One process tops out near 80 answers a second on the fake provider, and past that it fails with
  connection errors rather than refusals. Real answers are slow enough that slots run out first.

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

**Means:** the 95th percentile answer takes over 40 s, about twice what was measured.
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
In a real check on 2026-11-02, with the primary taken away, each question failed with a 502 in about
5 s. Answers came back with the next question after it returned, with no restart.

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

## CanaryRolledBack

**Means:** the canary rule found the canary worse than the champion and took it off traffic. The
service is back on the champion, so nothing is urgent.
**Check:** the last line of `results/service/registry-log.jsonl` holds the counts and the p-value.
The abstention and spend panels show what changed. A failure the two sides share, such as an outage,
does not fire the rule, because the comparison is with the champion over the same period.
**Do:** find out why before releasing that version again. Releasing it unchanged starts fresh counts,
so the rule would have to catch it a second time, at the cost of a second set of bad answers. If the
version should not have been rejected, remember the rule's false alarms are under 1%.

## CanaryRollbackFailed

**Means:** the rule fired and the canary gets no more traffic, but the registry file could not be
changed. The stop lives in the service's memory, so **a restart would put the canary back.**
**Check:** `uv run evals registry show` says what is wrong with the file. RegistryFileBad is probably
firing too.
**Do:** do not restart. Repair the file as under RegistryFileBad, then run the rollback command from
the top of this page.

## RegistryFileBad

**Means:** `configs/registry.yaml` cannot be used, so the service is running on the last good state
and **a rollback would not take effect**.
**Check:** `uv run evals registry show` says what is wrong: bad YAML, or a prompt text that no longer
matches its recorded hash.
**Do:** `git checkout configs/registry.yaml`, then set the pointers again with `evals registry`. If a
prompt's text was edited in place, that is the bug: a change to a prompt is a new version.
