# Phase 8 drill 1c: a canary signal for answers that read no page

**Summary.** The postmortem of 2026-11-02 left one blind spot open. A prompt that told the agent not
to read pages gave 1 bad answer in 12 under the canary rule, so it would very likely pass a canary.
Its answers came back cited, but they rested on search snippets alone. This change widens the rule's
one signal to see that, and this drill tests the change on the real model. It is committed before
the pilot and the drill run.

**Written:** 2026-11-03, before the pilot and the drill. The change was agreed the same day.

## The change

- **A bad answer is now one that errored, abstained, cited nothing, or read no page,** meaning no
  successful `read_page` call. It is still one signal and one test, so the false-alarm rate of under
  1% per canary is unchanged: the canary and the champion are graded the same way, and Fisher's
  exact test compares them.
- **It should cost the champion nothing.** On its stored offline runs the champion read at least one
  page for every question it answered: none of its 349 dev answers or 133 test answers read no page.
  So its share of bad answers, and with it the power table in `docs/plans/phase-8-llm-ops.md`
  part 6, should not move. The drill records do not hold pages read, so the champion's rate on
  simulated questions is first measured here.
- **The service reports it.** Each answer's response and log line now carry `pages_read`.
- **Its limit.** It sees an agent that stops reading, not one that reads a page and then ignores it.

## Step 1: measure the fault first

The phase 8 pilot, rerun for the champion and the no-reading prompt only, on the same 12 lookups,
now recording pages read (`results/service/drills/pilot-1c.json`). The drill runs only if at least
10 of the 12 no-reading answers are bad under the widened rule, and at most 2 of the 12 champion
answers are.

## Step 2: a free rehearsal

The same drill on the fake provider, which is taught to answer from the search result alone under
the no-reading prompt. It checks the plumbing and nothing more: the fake is told what the prompt
does, which is why it could not test drill 1's prediction.

## Step 3: the drill

**The fault.** `drill-no-reading` is the champion with one line changed. "2. Use read_page to read
the pages you will rely on. Never cite a page you have not read." becomes "2. To save cost, do not
use read_page. Work from the search results alone."

**Setup.** As in the common section of `docs/plans/phase-8-drills.md`: the same questions, 0.45
questions a second, the canary live at 20% after 25 questions, the same rule, the same isolated
registry. The drill script only releases the canary.

**Predictions.**

- The canary's share of bad answers is at least 90%.
- The champion's share stays between 10% and 20%, as in drills 1 and 1b, because it reads a page
  for almost every answer.
- The rule fires at the first look, at 20 canary answers, about 5 minutes after the canary goes
  live.

**Pass marks.** All must hold.

1. The rule fires by the third look.
2. The last line of the drill's registry log is an `auto-rollback` by `canary-rule`, and the champion
   is still `v3`.
3. No question sent after the rollback was seen is answered by the canary.
4. Questions that fail with a 5xx are at most 2% of those accepted.
5. `configs/registry.yaml` is unchanged.
6. Every answered response carries `pages_read`.
7. The pilot and the drill together spend at most $1.00, the drill at most $0.75.

## Result (2026-11-03): passed, 7 of 7 marks

**The pilot** (`results/service/drills/pilot-1c.json`, $0.04): the no-reading prompt gave 11 bad
answers in 12, and all 11 read no page. The champion gave 1 bad in 12, an errored answer, and
read a page every time. Both met the bar, so the drill ran.

**The rehearsal** (`results/service/drills/rehearsal-1c-no-reading.json`, free): 20 of 20 canary
answers bad, rolled back after 267 s. The plumbing works.

**The drill** (`results/service/drills/drill-1c-no-reading.json`, $0.24):

| | Answers | Cited, read no page | Abstained | Good | Bad |
|---|---|---|---|---|---|
| Canary | 20 | 9 | 6 | 5 | 15 |
| Champion | 114 | 0 | 14 | 100 | 14 |

The rule fired at the first look: "15 of 20 canary answers bad, 6 of 66 on the champion, p =
0.00000". The service removed the canary by itself 193 s after it went live.

| Mark | Result |
|---|---|
| 1. Fires by the third look | Met: the first |
| 2. Rollback by `canary-rule`, champion still `v3` | Met |
| 3. No canary answer after the rollback was seen | Met: 0 |
| 4. 5xx at most 2% | Met: 0 of 143 sent (9 were refused at capacity with a 429) |
| 5. `configs/registry.yaml` unchanged | Met |
| 6. Every answer carries `pages_read` | Met: 134 of 134 |
| 7. At most $1.00 in all, the drill at most $0.75 | Met: $0.27, the drill $0.24 |

**Against the predictions.** The champion's share of bad answers was 12.3%, inside the 10% to 20%
predicted, and the rule fired at the first look, after 3.2 minutes rather than about 5. The
canary's share was 75%, short of the 90% predicted: 5 of its 20 answers read a page despite the
prompt, against 1 of 12 in the pilot.

**What the change bought.** Graded by the old rule, the same 20 canary answers hold 6 bad ones, and
the first look would have read 6 of 20 against 6 of 66, p = 0.028. That is above the per-look bar
of 0.0033, so the old rule would not have fired there. The 9 cited answers that read no page made
the difference.

**What it cost the champion.** None of the champion's 100 answered questions read no page. Its 10
answers with no page read were all abstentions, which were bad under the old rule anyway. So on
simulated questions too, as on the 482 offline answers, the wider signal adds nothing to the
champion's count.

The phase 8 ledger stands at $3.43 of $10.

## What this run can and cannot show

It shows the widened signal catching a prompt that stops the agent reading, end to end on the real
model, and undone with no person involved. It says nothing about a prompt that reads pages and
answers worse. The gate before a release and the daily alert of phase 7 guard against those.
