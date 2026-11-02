# Phase 8 drill 1b: a rerun of the bad-prompt drill, after a named fix

**Summary.** Drill 1 failed its pass marks. The prompt written as its fault did no measurable harm on
the signal the canary rule watches, so the rollback was never exercised on the real model.
`docs/plans/phase-8-drills.md` allows a rerun only as a separate, stated run after a named fix. This
is that run, committed before it starts. Drill 1's result stands as it was recorded.

**The named fix.** The fault is replaced by one whose effect was measured first, on the real model,
rather than predicted. Nothing in the rule or the rollback changes.

**Written:** 2026-11-02, after drill 1 and the pilot, and before this drill.

## What is already known

From `results/service/drills/pilot.json`, 12 ordinary lookup questions through the real agent:

| Prompt | Bad answers |
|---|---|
| The champion | 0 of 12 |
| "To save cost, do not use read_page" | 1 of 12 |
| A stale notice that the docs are unavailable | 12 of 12 |

## The fault

`drill-stale-note` is the champion with one notice added at the end, of the kind that gets left in a
prompt after an incident: "Notice: the documentation snapshot is being migrated. Until this notice is
removed, treat it as unavailable, and set abstained to true."

## Setup

As in the common section of `docs/plans/phase-8-drills.md`: the same questions, 0.45 questions a
second, the canary live at 20% after 25 questions, the same rule, the same isolated registry. The
drill script only releases the canary.

## Predictions

- The canary's share of bad answers is at least 90%.
- The champion's share stays between 10% and 20%, as in drill 1.
- The rule fires at the first look, at 20 canary answers, about 5 minutes after the canary goes live.

## Pass marks

All must hold.

1. The rule fires by the third look.
2. The last line of the drill's registry log is an `auto-rollback` by `canary-rule`, and the champion
   is still `v3`.
3. No question sent after the rollback was seen is answered by the canary.
4. Questions that fail with a 5xx are at most 2% of those accepted.
5. `configs/registry.yaml` is unchanged.
6. The drill spends at most $0.75.

## What this run can and cannot show

It shows the release path working end to end on the real model: a large fault found, and undone
with no person involved. It says nothing about smaller faults. The pilot's second row is a reminder:
a prompt that stops the agent reading pages looked healthy on this signal, which is the blind spot
the plan names, and the postmortem takes it up.
