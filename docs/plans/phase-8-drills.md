# Phase 8 drills: what was expected, written before they ran

**Summary.** A drill proves something only if its pass marks were set before its result was known.
This file holds the setup, the predictions and the pass marks of each drill in
`docs/plans/phase-8-llm-ops.md`, part 6. It was committed before the first drill, so the git history
shows which came first, and it is not edited afterwards. Results go in the postmortem.

**Written:** 2026-11-02, after a free rehearsal of both canary drills on the fake provider and a
short real load test (`results/service/load-real.json`), and before any real drill.

## What is already known

From the real load test, on the same questions, with the champion on 8 slots:

| Measure | Value |
|---|---|
| Answers | 132 |
| Median answer time | about 11 s |
| 95th percentile answer time | 17 to 31 s across the four rates |
| Most answers per second | 0.59 |
| Abstained | 18 of 132 |
| Failed with no answer | 1 of 132 |
| Cost per answer | about $0.0018 |

So the champion's share of bad answers on these questions is about 14%, on a small sample.

## Common to the canary drills

- **Questions:** the 200 in `results/service/drills/questions.jsonl`, newly simulated, in a random
  order fixed by the drill's label, so a question can repeat. Nothing is read from `results/online/`.
- **Traffic:** 0.45 questions a second, arriving on the clock. That is below the measured ceiling,
  so most questions are accepted, and some are refused when arrivals bunch up.
- **Release:** after 25 questions on the champion alone, the canary goes live at 20% of traffic.
- **The rule:** as agreed, at 20, 40 and 60 canary answers, each look held to 0.33%.
- **Isolation:** `configs/service-drill.yaml`, with its own registry and log under
  `results/service/drills/`. The production registry, `configs/registry.yaml`, is not touched.
- **Who acts:** the drill script only releases the canary. It never rolls anything back.

## Drill 1: a bad prompt

**The fault.** `drill-fault` is the champion with one rule changed, the kind of edit a person might
think an improvement. "If the documentation you can reach does not answer the question, set abstained
to true" becomes "Set abstained to true unless the first page you read states the answer word for
word. Abstaining is always better than answering."

**Predictions.**

- The champion's share of bad answers stays between 10% and 20%.
- The canary's share is at least 50%.
- The rule fires at the first look, about 5 minutes after the canary goes live, and at the second
  look at the latest.

**Pass marks.** All must hold.

1. The rule fires by the third look.
2. The last line of the drill's registry log is an `auto-rollback` by `canary-rule`, and the champion
   is still `v3`.
3. No question sent after the rollback was seen is answered by the canary.
4. Questions that fail with a 5xx are at most 2% of those accepted.
5. `configs/registry.yaml` is unchanged.
6. The drill spends at most $1.50.

## Drill 2: a harmless change

**The change.** `drill-harmless` is the champion with one heading reworded, from "How to work:" to
"Work like this:". It should behave as the champion does.

**Prediction.** The rule stays quiet through all three looks. The chance of a false alarm is under
1%.

**Pass marks.** All must hold.

1. Three looks are taken and none fires.
2. The canary is still live after the third look, and `canary_passed_total` is 1.
3. Questions that fail with a 5xx are at most 2% of those accepted.
4. `configs/registry.yaml` is unchanged.
5. The drill spends at most $1.50.

## Drill 3: the primary provider goes down

This one needs the second provider's key, so it runs later, under the same marks.

**Setup.** `deploy/failover-drill.yml` puts a forwarder between the service and the primary. At 0.3
questions a second, the forwarder is stopped after 40 questions and started again after 80, out of
120. Nothing in the service changes, and it is not restarted.

**Predictions.** A failed call moves to the second provider within the same answer, so few questions
fail. The circuit opens after 3 failures in a row, and calls then skip the primary. It tests the
primary again after 30 seconds, and closes by itself once the primary is back.

**Pass marks.** All must hold.

1. Of the questions accepted while the primary is down, at least 95% are answered.
2. Those answers name `openai` as a provider.
3. `gateway_failovers_total` rises, and the `ProviderCircuitOpen` alert fires.
4. Within 2 minutes of the primary coming back, answers served by `anthropic` alone resume, with no
   restart.
5. The drill spends at most $0.50 with the primary.

## If a drill fails

A failed drill is reported as failed, and the postmortem is about that. It is not rerun quietly. Any
rerun is a separate, stated run, after a named fix.
