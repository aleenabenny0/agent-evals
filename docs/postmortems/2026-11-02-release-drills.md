# Postmortem: the release drills of 2026-11-02

**Summary.** The first real drill of the automatic rollback failed. A prompt written to be a fault
went live at 20% of traffic and did no measurable harm on the signal the canary rule watches, so the
rule stayed quiet and the rollback never ran. The prediction behind the drill was wrong, and the free
rehearsal could not have shown that, because the fake provider had been told what the prompt would
do. A rerun with a fault measured first passed: the rule fired at its first look and the service
removed the canary by itself in 5.2 minutes, after 20 bad answers. A harmless change stayed quiet.
Two defects were found along the way and fixed. One limit remains and is stated below.

This was a planned drill on simulated questions, in an isolated registry. No real user saw any of
it, and `configs/registry.yaml` never changed. This write-up is blameless: it is about how the system
and the process behaved. The pass marks were committed before each drill
(`docs/plans/phase-8-drills.md`, `docs/plans/phase-8-drill-1b.md`).

## Results against the pass marks

| Drill | Change released | Outcome | Marks met |
|---|---|---|---|
| 1 | An over-strict abstention rule | **Failed:** the rule never fired | 3 of 6 |
| 2 | A reworded heading | **Passed:** quiet through three looks | 5 of 5 |
| 1b | A stale "docs unavailable" notice | **Passed:** rolled back by itself | 6 of 6 |

| Drill | Canary bad answers | Champion bad answers | Rule | Spent |
|---|---|---|---|---|
| 1 | 2 of 50 | 29 of 256 | quiet at looks 1 and 2, look 3 not reached | $0.59 |
| 2 | 10 of 71 | 39 of 324 | quiet at looks 1, 2 and 3 | $0.74 |
| 1b | 20 of 20 | 29 of 153 | fired at look 1 | $0.32 |

Drill 1's three unmet marks were the rule firing, the rollback entry in the log, and the check on
canary answers after a rollback, which never came.

## Timeline

All times are UTC, on 2026-11-02, from `results/service/drills/registry-log.jsonl` and the phase's
ledger.

| Time | Event |
|---|---|
| 17:00 to 17:10 | Free rehearsal on the fake provider. Both canaries behave as designed. |
| 17:18 | 200 new drill questions generated, for $1.21. |
| 17:23 | Short real load test: answers take about 11 s, and an answer costs about $0.0018. |
| Before 17:26 | Pass marks committed (2dcfe5f). |
| 17:26:43 | Drill 1: `drill-fault` live at 20%. |
| About 17:31 | Look 1: 2 of 20 bad, 9 of 113 on the champion, p = 0.52. |
| About 17:36 | Look 2: 2 of 40 bad, 23 of 203 on the champion, p = 0.94. |
| 17:37:47 | Drill 1 reaches its 340 questions with 50 canary answers, so look 3 is never taken. |
| 17:39:44 | Canary dropped by hand, since the rule never fired. Drill 1 recorded as failed (1b4da27). |
| 17:41:06 | Drill 2: `drill-harmless` live at 20%. |
| 17:41:47 | A 12-question pilot of two new candidate faults, on the real model, for $0.04. |
| 17:55:21 | Drill 2 ends. The rule stayed quiet, with p = 0.79, 0.67 and 0.65. |
| 20:08:26 | Drill 1b: `drill-stale-note` live at 20%, after its marks were committed (0db7413). |
| 20:13:36 | The rule fires at look 1, and the service removes the canary. 310 s after going live. |
| 20:17 | A 10-question check of the failover forwarder exposes a stuck circuit metric. |

## Why drill 1 failed

**The fault was predicted, not measured.** The prompt replaced the abstention rule with "Set
abstained to true unless the first page you read states the answer word for word". The prediction
was at least 50% bad answers. The real figure was 4%. The agent's job is already to copy quotes word
for word, so the condition was easy to meet, and it did not abstain on any of 36 ordinary lookups.

**The rehearsal could not have caught this.** The fake provider abstains whenever it sees that
prompt, because it was built that way to exercise the rollback. So the rehearsal tested the mechanism
and quietly assumed the fault. That was a sound test of the mechanism, and no test at all of the
fault.

**The rule did what it should.** The champion's bad share came in at 11%, inside the predicted range,
and the canary looked no worse. On that signal, staying quiet was correct. What failed was the drill
design, and the rollback path simply went unexercised. The signal says nothing about whether the
canary's answers were accurate, so this does not show that the prompt was harmless.

## What went well

- **Writing the marks down first.** The result could not be argued away, and the rerun had to be
  named and separate. Drill 1 stands in the record as a failure.
- **A cheap pilot settled the next fault.** It cost $0.04 and removed the guess.
- **Every prediction in drill 1b held:** the canary at 100% bad, the champion at 19%, and the rule
  firing at the first look at about 5 minutes.
- **Queueing theory predicted the refusals.** At 0.45 questions a second on 8 slots, the Erlang B
  formula gave 8% to 11%, and the three drills refused 8.1%, 8.9% and 9.7%.
- **The isolation held.** The production registry never changed.

## What went badly

- **The first fault was chosen without a measurement,** at a real cost of $0.59 and about 15 minutes.
- **20 bad answers reached the traffic before the rollback.** The rule needs that many to decide.
  That is the agreed trade-off at 20%, and it is the size of the harm a large fault does before it is
  caught.
- **Look 3 was missed in drill 1.** The hash split gave 50 canary answers out of 306, where about 61
  were expected. The fix for drill 2 was to send more questions. A real release needs enough traffic
  to reach the last look, or the rule has less to say.

## Defects found and fixed

- **`SpendHigh` fired during drill 1.** Its threshold was a fixed $0.50, half of the production cap,
  while the drill's cap was $7. The service now exports its cap, and the alert reads half of that. It
  stayed quiet in drill 1b.
- **`gateway_circuit_open` could stay at 1 for good.** A call that began while the circuit was closed
  and ended after it opened did close it, but nothing reported the close, because the check used a
  reading taken before the call. `ProviderCircuitOpen` would have kept firing after a recovery. The
  breaker now reports its own close. Two new tests fail on the old code. This was found by a real
  10-question check that cost about a cent, and no offline test had caught it.

## The limit that remains

**This rule sees a broken prompt, not a worse one.** In the pilot, a prompt telling the agent not to
read pages at all gave 1 bad answer in 12, so it would very likely pass a canary. Its answers came
back cited, though they rested on search snippets alone. The plan names this blind spot, and this is
it measured. The gate before a release is what guards accuracy, and phase 7's daily alert watches for
small regressions. The rule is a last check for large breakage, and should not be read as more than
that.

*Update, 2026-11-03.* This blind spot is closed for the case measured. The signal now also counts an
answer that read no page, and drill 1c released the same "do not read pages" prompt. The rule fired
at its first look, 15 of 20 canary answers bad, where the old signal on the same answers would have
stayed quiet (`docs/plans/phase-8-drill-1c.md`). A prompt that reads pages and answers worse is still
invisible to it.

The signal also counts every abstention as bad, including right ones on questions the docs cannot
answer. That costs nothing when comparing like with like, since both sides see the same mix. But a
prompt that stopped abstaining would look better on this signal, not worse. The drills did not show
this happening: the canaries abstained on every outside-the-docs question they got, on small counts.

## Actions

| Action | Status |
|---|---|
| Measure a fault with a small pilot before a drill relies on it | Done (`scripts/drill_pilot.py`) |
| Make the spend alert follow the configured cap | Done |
| Report a circuit's close from the breaker itself | Done |
| Send enough questions for a drill to reach its last look | Done |
| Run the failover drill on the second provider | Not run: no budget for a second paid provider |
| Decide whether to add a signal for answers that stop reading pages | Done 2026-11-03: added; drill 1c passed |
