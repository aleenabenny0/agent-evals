# Phase 8: the docs agent as an operated service (Brief 01, LLM Ops)

**Summary.** The docs agent has only ever run as a batch job. This phase runs it the way a
production LLM feature is run: behind an HTTP service, with versioned prompts that can be switched
without a deploy, failover to a second model provider, traces and metrics, an eval gate that blocks
an unmeasured change from reaching `main`, a canary with an automatic rollback, and a load test. The
phase ends with a drill, in which a deliberately bad prompt is released to a share of traffic and
the system has to catch it and roll it back, and with the runbook and the postmortem that drill
produces. Everything runs on one machine under Docker Compose. Estimated cost about $6, capped at
$10, plus a small balance with the second provider.

**Status:** proposed 2026-11-01, awaiting approval of the decisions at the end. The parts that need
no decision and no paid call are being built first.

**Goal.** Show that a change to this agent can be released safely: measured before it merges,
watched while it rolls out, and undone in seconds when it goes wrong, with the cost of each step
known.

## What exists, and what is missing

| Brief 01 asks for | Exists today | Missing |
|---|---|---|
| A service | `DocsAgent.run` behind the `Agent` protocol | An HTTP surface, limits, health checks |
| A prompt registry | `SYSTEM_PROMPTS` by version, chosen by `docs_agent.prompt_version` | A live pointer that changes without a deploy, and its history |
| A gateway with failover | `LLMClient`, one provider | A second provider, timeouts, a circuit breaker |
| Traces and metrics | Trajectories in DuckDB after a run | Live traces, metrics, a dashboard |
| An eval gate in CI | `evals gate` run by hand (ADR 0008); lint, types and tests in CI | A check that a changed agent was gated |
| Canary and rollback | The online alert over daily runs (phase 7) | Per-version traffic, a fast rule, an automatic rollback |
| A load test | Nothing | A capacity figure and service objectives |
| Runbook and postmortem | Nothing | Both, from a real drill |

The agent itself does not change. Its accuracy was measured in phases 2 to 5, and this phase adds no
claim about it, except one number the failover makes necessary: how much worse answers get on the
second provider.

## Part 1: the service

`src/agent_evals/service/`, a FastAPI app. It talks to the agent only through the `Agent` protocol,
and every model call still goes through `LLMClient`.

- `POST /v1/ask` takes a question and returns the answer, its citations, whether the agent
  abstained, and the request id, prompt version, provider and cost of that answer.
- `GET /healthz` says the process is up. `GET /readyz` says it can serve: the snapshot is loaded,
  the registry is readable and at least one provider's circuit is closed. `GET /metrics` is for
  Prometheus.
- **Limits, so that overload degrades instead of collapsing.** A cap on concurrent answers, past
  which the service returns 429 at once, a timeout per answer, a question length limit, and a daily
  spend cap, past which it returns 503. All are settings in `configs/service.yaml`.
- **No call cache when serving.** The cache makes evals reproducible by replaying replies, which in
  a service would freeze answers. Cost control comes from the limits.
- **Privacy.** Logs and traces carry a hash and the length of a question, never its text, and metric
  labels never hold free text.

## Part 2: the prompt registry

`configs/registry.yaml` holds the live pointers: `champion`, an optional `canary`, and the canary's
traffic share. Each version's text stays in `prompts.py`, so it is reviewed like code, and a pointer
may only name a version whose hash is on record. `evals registry show`, `canary`, `promote` and
`rollback` change the pointers and append to `results/service/registry-log.jsonl`: who, when, why, from
to. The service rereads the file when it changes, so a rollback takes effect with no restart. A
request is assigned by a hash of its id, so the split is deterministic and reproducible.

## Part 3: the gateway and failover

`ResilientClient` is an `LLMClient` that wraps others in order of preference, so nothing above it
changes.

- A timeout on every attempt, a bounded number of retries, and retries only on errors worth
  retrying.
- A circuit breaker per provider: after repeated failures it stops sending for a cool-down, then
  lets one request test the provider. So an outage costs one timeout, not one per request.
- **The failover point is one model call, not one answer.** An answer that has done three steps on
  the first provider continues on the second. So `OpenAICompatibleClient` translates each call:
  requests stay in the shape the agent already uses, and the adapter converts the tools, the tool
  calls and the tool results both ways. It speaks the OpenAI-compatible API, so the provider behind
  it is a base URL and a model name in the config.
- A failover is visible: the response names the provider, and a metric counts every switch.
- **What a failover costs in quality is measured, not assumed.** The dev suite runs once on the
  second provider and goes through the existing graders, so the runbook can say what users get
  during an outage. This number describes the failover path, and nothing is promoted on it.

This moves up a decision of 2026-10-17, which kept one provider until the routing phase. That
decision predates this plan, and real failover cannot be shown with one provider. ADR 0010 records
the change.

## Part 4: traces, metrics and a dashboard

OpenTelemetry spans for a request, each agent step, each model call and each tool call, exported to
Jaeger. Prometheus metrics for the four golden signals (traffic, latency, errors, saturation) and
for what is particular to an LLM service: tokens, cost, abstentions, rejected submissions, failovers
and circuit state, each by prompt version and provider. A provisioned Grafana dashboard reads them.
Docker Compose starts the service, Prometheus, Grafana and Jaeger with one command. Bound to
localhost only.

## Part 5: the eval gate in CI

CI holds no API key, so it cannot run the agent, and giving every pull request a paid run would
spend money on changes that do not touch the agent. A **gate receipt** does the job with no key. The
agent's version fingerprint already changes with anything that can change behavior. CI recomputes
it, and fails when it has no committed receipt in `results/gates/receipts/`. A receipt is written by
`evals gate` after a real run, and records the version, the runs, the verdict and the commit. So a
change that alters the agent cannot merge unmeasured, a change that does not is untouched, and CI
stays free.

## Part 6: the canary and the rollback drill

A canary sends a share of live traffic to a new version. Phase 7's alert compares whole days, far
too slowly for this, so the canary needs a rule of its own, on signals that code can grade at once:
errors, no citations, rejected submissions, abstention, fabricated citations and latency.

**(agree first)** The rule is a statistical decision, so it is agreed before any code, as in phase
7. The proposal is decision 6:

- **One signal.** A bad answer is one that errored, abstained, or came back with no citation. One
  signal needs no correction across signals, and every one of those is graded by code at once.
- **Against the champion at the same time.** The canary's share of bad answers is compared with the
  champion's over the same period, so a change in the questions moves both.
- **Three looks,** after 20, 40 and 60 canary answers, with a one-sided Fisher exact test at each,
  the test the online monitor already uses. Each look is held to 0.33%, so an overall false alarm
  stays under 1% however many of the looks are taken.
- **A canary that reaches the third look without firing is left in place for a person,** since
  passing shows only the absence of a large fault.

Simulated in `scripts/canary_power.py` (20,000 rounds at 20% traffic,
`results/service/canary-power.json`), the chance the rule fires:

| Bad answers, champion | Canary no worse | 10 points worse | 20 points worse | 30 points worse | 50 points worse |
|---|---|---|---|---|---|
| 5% | 0.3% | 38% | 92% | 99.7% | 100% |
| 10% | 0.4% | 22% | 80% | 99% | 100% |
| 20% | 0.4% | 14% | 62% | 95% | 100% |

So it catches a badly broken prompt quickly, and at 50 points worse usually at the first look, after
about 100 answers served. It often misses a change of 10 points, and that is the honest limit of 60
answers. Phase 7's alert, over whole days, remains the guard against a small regression. Like that
alert, this rule cannot see a fault that only a judge would catch: a prompt that gives confident,
cited, wrong answers looks healthy here, which is why the gate comes first.

**The drill.** Simulated questions run through the service, with the canary at 20%. The fault, the
expected outcome and the time to detect are pre-registered and sealed before the run. The fault is a
deliberately bad prompt registered for the drill only, and it never touches `champion`. The system
must detect it, roll back by itself and keep serving, and a second drill with a harmless change must
stay quiet. Failover gets its own drill, in which the primary provider is blocked mid-traffic.

**Blindness.** The drill uses newly simulated questions. It reads nothing from `results/online/`, so
the 376 fresh answers kept for a later judge round stay unread.

## Part 7: the load test

A load generator against the running service. Almost all of it runs with a fake provider that sleeps
for realistic latencies, so finding the capacity costs nothing. A short run against the real API
then checks that the fake was realistic. The output is a capacity figure on this machine, the point
where latency turns upward, how the limits behave past it, and service objectives set from what was
measured.

**Measured so far, on the fake provider (2026-11-01), so at no cost.** The tools, the search and the
submission checks are real, and the fake waits about 1.7 seconds per model call, so an answer takes
about 5 seconds (`results/service/load-*.json`).

| Slots | Offered per second | Answered per second | Refused | 95th percentile answer |
|---|---|---|---|---|
| 8 | 0.5 | 0.46 | 0% | 7.5 s |
| 8 | 1.2 | 0.93 | 16% | 8.9 s |
| 8 | 2.3 | 1.29 | 39% | 8.9 s |
| 8 | 5.0 | 1.50 | 68% | 8.1 s |
| 8 | 198 | 1.52 | 99% | 7.9 s |
| 16 | 1.0 | 0.95 | 0% | 7.7 s |
| 16 | 1.8 | 1.65 | 1% | 8.2 s |
| 16 | 3.3 | 2.20 | 19% | 8.9 s |
| 16 | 5.1 | 2.77 | 41% | 7.7 s |

- **Overload degrades the way it was designed to.** At 130 times capacity the service refused 7,307
  requests with no errors, and the answers it did give took as long as at low load.
- **Capacity is slots over answer time,** about 1.5 answers a second at 8 slots and 3 at 16. The
  share refused follows the Erlang B formula from queueing theory: at 2.3 a second on 8 slots it
  predicts about 40% and the service refused 39%. So the slots a traffic level needs can be worked
  out, and the real limit on slots is the provider's rate limit.
- **One process has a ceiling of about 80 answers a second** on this 8-core machine, because search
  is Python work on one interpreter. With the model's latency removed and 64 slots, the service
  held up to 83 a second. Past that it fails badly: connections are refused and answers take tens
  of seconds. That is about 55 times today's capacity, and a real model's latency keeps the slots
  full long before it is reached, but going near it would take more processes.
- **The real provider is still to be measured,** which is what the short paid run is for.

## Part 8: the runbook and the postmortem

`docs/runbook.md`: for each alert, what it means, how to check it and what to do.
`docs/postmortems/`: one blameless postmortem for the drill, with a timeline from the logs. If a
drill goes wrong, that is the postmortem.

## Build order, and what each part needs

| Part | Needs a decision | Paid |
|---|---|---|
| 1. Service | no | no |
| 2. Registry | no | no |
| 3. Gateway, with a fake second provider in tests | no | no |
| 3. The dev suite on the second provider | decision 1 | yes |
| 4. Traces, metrics, dashboard | no | no |
| 5. Gate receipt | no | no |
| 6. The canary rule | the rule is agreed first | no |
| 6. The drills | decisions 1 and 2 | yes |
| 7. Load test, fake provider | no | no |
| 7. Load test, real provider | decision 2 | yes |
| 8. Runbook and postmortem | follows the drills | no |

## Cost and human time

At about $0.005 per answer, from the dev runs.

| Step | Answers | Estimate |
|---|---|---|
| Dev suite on the second provider | 130 | about $1.50, at that provider's prices |
| Canary drills, a bad prompt and a harmless change | about 600 | about $3 |
| Failover drill | about 100 | about $0.50 |
| Load test against the real API | about 200 | about $1 |
| **Total** | about 1,030 | **about $6, capped at $10** |

Human time: about 15 minutes for the decisions, and a few minutes to create a key with the second
provider and set a spending limit there.

## Risks

- **The second provider answers worse, or handles the strict tool schemas differently.** That is why
  its answers are measured. If it cannot complete the loop, the failover is reported as not working.
- **The canary rule could fire on noise, or miss a real fault.** Its false-alarm rate is fixed in
  advance and tested on the harmless drill, and its blind spots are stated.
- **A rollback that depends on the failing system.** The rollback only rewrites a local file, and it
  is tested with the provider down.
- **Scope.** This is the largest phase by surface area. Each part stands alone and is committed
  alone, so stopping after any part leaves working software.

## Decisions (to approve)

Recommended defaults are marked. Approval covers the paid steps up to the cap.

1. **The second provider.** (a) OpenAI, or another OpenAI-compatible provider, with a key and a low
   spending limit set there. *Recommended.* It is real failover, at about the same price per answer.
   (b) A local model. Free, but this machine has no GPU, so an answer would take minutes, which is
   not a failover anyone could use. (c) A fake provider only. Free, but it shows the mechanism and
   not the failover.
2. **Budget.** (a) A ledger capped at $10 for the Anthropic spend, separate from the judge rounds.
   *Recommended.* (b) Another cap.
3. **Where it runs.** (a) Docker Compose on this machine, bound to localhost. *Recommended.* It
   costs nothing and needs no cloud credentials. (b) A cloud host, which adds cost and an exposed
   endpoint.
4. **CI.** (a) The gate receipt, with no API key in GitHub. *Recommended.* (b) A paid run on every
   pull request that changes the agent, with a key stored as a GitHub secret.
5. **Rollback.** (a) Automatic on the agreed rule, with promotion always done by a person.
   *Recommended.* (b) The rule only raises an alert, and a person rolls back.
6. **The canary rule.** (a) The rule in part 6, as simulated. *Recommended.* (b) More looks or more
   answers, which catches smaller faults for more money per release.
