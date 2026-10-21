# Phase 7: online evals with a simulated user (Brief 02 M7)

**Status:** plan v2, 2026-10-18. v1 was reviewed by two isolated agents: one on statistics, one on
the simulated user and the demo. Both rated it "weakened", with 21 fixable problems, and this
version takes them all in (the table at the end). The online-case grader guards (part 2) and the
alert rule (part 3) touch `graders/` and `stats/`, so they are agreed with Aleena before they are
coded. Part 1 needs no decision.

**Goal.** Show the docs agent monitored the way it would be in production. Fresh questions with no
answer key are graded only by checks that need none, summarized per simulated day, and watched by
an alert. The alert must stay quiet on normal days, fire on a deliberate regression, and be
tested blind.

## 1. Simulated user (`evals simulate DAY`)

- **Exact quotas.** Every day has the same mix: 5 personas with 20 questions each. Each persona
  writes 14 lookup or how-to questions, 2 comparisons, 2 false premises and 2 outside-snapshot
  questions, so 70/10/10/10. A slot is retried under a new sub-salt when the generator gives no
  question, when an outside-snapshot question leaves out its service, or when the question is a
  near-duplicate (word-set Jaccard 0.6 or more) of an earlier one that day, with the clash quoted
  back. After 6 tries an in-snapshot slot moves to a redrawn page, keeping persona and intent.
  Every day so has the same N and mix, or fails loudly.
- **Personas:** a new developer; an on-call engineer, terse, with error messages and typos; a
  security reviewer; a cost-conscious manager; a non-native English speaker. At least one in four
  how-to questions is phrased as a symptom ("I get AccessDenied when ..."), not in the docs' own
  words.
- **Sources.** Each in-snapshot question comes from one source page, drawn uniformly from the
  allowed pages. A page is excluded if it is gold evidence for a test case (72 pages) or holds a
  test case's support sentence word for word (17 more, mostly quota pages), so the
  test split stays clean even if online failures later feed an iteration. 245 pages remain. Every
  page has an area in the committed `configs/online-area-map.json`, and the area mix is reported
  per day.
- **What the generator sees** (v2, after the pilot; see "Pilot and generator v2" at the end).
  For a lookup or comparison, `claude-sonnet-5-5` sees the page's title and one section of the
  page, and a checker call confirms that the section answers every part of the question before it
  is kept. For a false premise it sees the title and headings and one prose sentence with a
  checkable fact (a number with a unit, or a default, limit or prohibition), verified to be on the
  page word for word. The question must contradict it, and it is stored as the question's minimal
  key (`contradicts`, which becomes `gold.support` in part 2).
- **Outside-snapshot services.** Each question names a service that no snapshot page mentions, in
  its title, URL or text, and that no test question names, so declining it is known to be right.
  `evals validate` checks only titles and URLs: five of the twelve `suites.held_out_services` are
  mentioned in passing (Outposts on ten pages, as in "On Outposts, gp3 volumes support sizes up
  to 16 TiB"), and two more are named by test questions. So `simulate.py` adds eight candidates,
  every candidate is checked when a day is planned, and each dropped one is recorded with its
  reason. 13 names remain. Snowball and Snow Family are one product family, so a day names each
  family at most once (generator v2.1): a day's 10 outside questions name 10 different products,
  from 12 families.
- **Recorded per question:** source page, persona, intent, area, and the share of its content
  words (stopwords dropped) that appear in the page's title or headings.
- **Storage.** `results/online/traffic/day-NN.jsonl`, outside `suites/`, so snapshot checks and
  evidence indexes never load it. Generation is cached by day, slot and attempt, so a day can be
  rebuilt exactly. `day-NN.meta.json` records hashes of the questions and of every input
  (snapshot, test split, area map, slot plan), and `evals simulate` refuses to overwrite a day
  with different questions unless given `--force`.
- **Pilot before day 1** (`evals pilot-review 0`). Aleena reviews 30 generated questions, 6 per
  persona, stratified by intent: 3 lookups, 1 comparison, 1 false premise and 1 outside-snapshot
  question each, so every intent gets at least 5 reviews (a plain random 30 held one
  outside-snapshot question). She marks each realistic or not, and answerable from the snapshot
  or not; outside-snapshot questions should be "not", and for a false premise the question is
  whether the snapshot shows the premise wrong. The tool shows the two passages of the source
  page that the agent's own search ranks best, with the page link, because they can miss the
  answer; she answers "unsure" rather than guess. Each answer is saved as it is given.
- **The pilot's bar, fixed before any answer exists.** The realistic share is weighted by each
  intent's share of a day (70/10/10/10), because the sample over-samples the rare intents. It is
  decided only once all 30 are reviewed. Below two thirds, the generator prompts are revised
  before day 1. The rates are published, per intent.

## 2. Online runs (grader guards, approved by Aleena 2026-10-19)

**Built** (commit 1e96ed6, then the review's 16 fixes): `evals online-run DAY [--no-judge]` turns
the day's traffic into `results/online/cases/online-day-NN.jsonl`, runs the docs agent once per
question and judges the answers. A run is online because its cases are, recorded with the run,
never because of a file name; a file or run that mixes online and offline cases is refused.
`evals judge` refuses an online run unless the judge is `2cf75fa03e09`, and online-run checks that
before paying for the agent. Judge, regrade and label refuse an online run whose cases file has
changed since it ran. The gate refuses online runs, which have no reference answers. Each online
run records its day, its config and whether that was the champion's.


- **Case model.** New values: suite "online", split "online" and provenance "simulated", which is
  not a draft. `gold` may be absent, but only on online cases. Every place that reads `case.gold`
  checks for that. The evidence metrics are skipped, and the judge's "responsive" check is skipped,
  because there is no reference answer. An online case with an empty `Gold` object is rejected
  when it loads, so nothing can score it correct by accident.
- **Keyword grader.** On an online case it records only `abstained`. It never records `correct`,
  `wrong_abstention` or `missed_abstention`. Tests cover both rules.
- **Graded:** citation validity; `uncited_answer` and `malformed_output`; `unsupported_entity`;
  trajectory signals; abstention, errors, latency and tokens; and the judge's support verdicts,
  pinned to judge `2cf75fa03e09` with rubric v2.
- **Kept apart.** Online runs never become `latest_run_id`, so `evals report` keeps showing suite
  runs.

## 3. Monitor and alert (`evals monitor`, approved by Aleena 2026-10-19)

**Built** (commit 96284ab, then the review's fixes; see "Code review of parts 3 and 4"):
`evals monitor --freeze` runs the leave-one-day-out check on days 1 to 5 and, if no day alerts
against the other four, freezes them in `results/online/reference.json`. A frozen reference is
replaced only with `--refreeze`, which moves everything checked against the old one to
`results/online/archive/`. If the runs are the same, it re-records the reference: the freeze
date stands, the new record points to the one it replaced (a chain back to the first), and the
judge check's sample and result stay in place, since they belong to the runs; alerts are
archived either way. `evals monitor DAY` checks a later day and records it in
`results/online/alerts.json`. It exits 0 when the day is clear and 1 on an alert. It exits 3 when
nothing alerts but the day is not cleared, because a family metric went untested or the judge is
in the family and untrusted that day. It exits 2 when the day cannot be checked: its run is not
complete, is not judged in full by the pinned judge, or its cases or traffic file has changed
since it ran. Each reference day must have exactly one complete champion run; which of two runs
counts is a decision, never a default. `evals judge-check label` walks Aleena through the 30
blind labels: from each reference day, 3 answers the judge passed and 3 it failed, drawn with a
seed per day and shown in an order that reveals neither. `evals judge-check score` counts the
judge's false passes, and reports its false fails. If the labels pass, it reruns the
leave-one-day-out check with judge_supported in the family, and judge_supported may alert only
if that stays quiet too.

**Reference frozen 2026-10-19** on the real runs of days 1 to 5, before any later day ran. No
reference day alerted against the other four, with the 6-metric family (judge_supported waits
for the labels). The closest calls stayed under their floors: day 3's latency was +11% with p
0.016, against a 25% floor, and day 1's in-scope declines were +4.0 points with p 0.071, against
a 5-point floor. All five runs were complete, with every cited answer judged by `2cf75fa03e09`.
Re-recorded 2026-10-20 with decision 5's blind spots: the same runs, the same p-values and the
same decisions. The first record is in `results/online/archive/2026-10-20T172459Z/`.

- **Reference.** Days 1 to 5, frozen. Before any later day is judged, a leave-one-day-out check
  runs: each reference day is tested against the other four with the full rule. It must raise no
  alert, or the reference is too noisy and the demo stops. Five looks are a strict test: by the
  statistics review's simulation, five days drawn from one distribution still stop here about
  10% of the time, and up to about 17% with the day-to-day variation seen in the real days.
  Days 1 to 5 passed, so the rule stays as approved, and a stop cannot be re-rolled.
- **Alert family,** one-sided in the bad direction, Holm-corrected across 7 metrics, each with a
  practical floor:

| Metric | Bad direction | Test | Floor |
|---|---|---|---|
| judge_supported, over answers judged | down | Fisher exact | 5 points |
| uncited_answer | up | Fisher | 5 points |
| abstention on lookup and comparison questions | up | Fisher | 5 points |
| error rate, over all attempts | up | Fisher | 2 points |
| model_calls per attempt | up | permutation, mean | 0.5 calls |
| latency, uncached attempts only | up | permutation, mean of log | 25% |
| tokens per answer | up | permutation, mean of log | 25% |

- **Fabricated citations** get their own safety rule, outside Holm, because at this volume Holm
  would only fire at six times the baseline. The rule is one-sided Fisher, p < 0.01, with no floor.
- **Reported but never alerting:** judge_supported over all in-scope questions (lookups and
  comparisons), counting abstained, uncited and errored answers as not supported, which shows
  the denominator effect;
  answering an outside-snapshot question (the declines are known to be right); false-premise
  outcomes per intent; judge_core_supported, which is advisory; median answer length; and the
  share of answers naming their source page.
- **Judge trust.** If median answer length or the source-naming share moves more than 25% from
  the reference, that day's judge_supported is marked untrusted. The 25% is relative to the
  reference value, in either direction; with source naming near 96%, only a fall below about
  72% can trip it. An alert still stands, but a non-alert does not clear the day.
- **Judge check.** Before judge_supported may alert at all, Aleena labels 30 answers from
  reference days blind: from each day, 3 the judge passed and 3 it failed (decision 6). A plain
  random 30 would hold only about 7 or 8 judge passes (10 in the draw of 2026-10-19), since the
  judge passes about a quarter of online answers, and a false pass can happen only on those.
  The judge must pass at most 2 she fails, of its 15 passes: the bar of iteration 4's spot
  check. Its false fails are reported too. The record of the check is the results database:
  the sample, when drawn, and the first complete score, when made, are kept on every reference
  run. `results/online/judge-check-sample.json` and `judge-check.json` are only their published
  copies, restored from the database if they go missing or are edited. Every rule reads the
  database, never the files:
  - The sample leaves out the answers the dashboard publishes with the judge's verdict (each
    day's five least supported), and the dashboard never shows a sampled answer.
  - Only `judge-check label` draws the sample, and never once the reference runs carry
    anyone's labels.
  - The sample carries a digest of the judge's grades on it, reasons included. Labeling and
    scoring are refused if they changed, which catches any re-judging after the draw.
  - Until the first complete score, the judge's verdicts on the sampled answers are held back,
    in every run that holds them (a replay of a reference day included), by every command that
    would show them: `label --show-judge`, `judge-validate`, `judge-probe`, `compare` on a
    judge metric, and `trace` with any config. Only a deliberate look into the database or the
    call cache itself could still reach them.
  - Only Aleena's labels count.
  - The first complete score is final, because its result shows the judge's verdicts and a
    later relabel and rescore would no longer be blind. No file written, edited, moved or
    deleted can make, change or reopen it, and nor can a re-record of the same runs.
  - judge_supported is in the alert family only while the recorded score passed and the
    judge's grades on the sample are still the ones it was scored on: a re-judge afterwards
    takes it out. The monitor and dashboard say which reason keeps it out.
  - The check runs only on the frozen reference: each day's one complete champion run at the
    draw, and the runs it was drawn on after. `online-run` refuses a frozen reference day. The
    records are written once, enforced by the store itself and written all together, so
    nothing, not even a judge whose config names it after a record, can overwrite them. A run
    belongs to one judge check.
  - No test can touch the real `results/online/`: each test's monitor files go to a temporary
    folder, and the test run fails if a real file changed.

  **Drawn 2026-10-21T00:06:59Z**, before any label existed on the reference runs (commit
  a6c3a71, `results/online/judge-check-sample.json`). It has 6 answers a day, 3 the judge
  passed and 3 it failed, none of them on the dashboard, and is recorded on all five runs. Run
  end to end on a copy of the real data, with scripted labels, scoring refused until all 30
  were in. It then reran the leave-one-day-out check with the judge in the family on the real
  days, which stayed quiet, and a second score only reported the first.
- **Mechanics.** Permutations use at least 20,000 draws, with p = (k+1)/(R+1). Any day with cache
  hits is refused for latency. A metric that cannot be tested that day (refused, or no data)
  leaves the Holm family, the report gives the number tested, and the day is not cleared.
- **Blind spots** (decision 5). Every metric, the fabricated-citation rule included, publishes
  its detectable rise: the smallest true rise that would alert on 8 days in 10, its floor
  included, at the day's n against the reference. For rates it is exact: the fewest bad
  outcomes that pass Fisher's test and the floor, and the binomial chance of reaching them. For
  means it uses the normal approximation. Simulated on the real reference days, under the real
  rule (the permutation test plus the floor), days whose true rise equals the blind spot alert
  81% of the time for model calls, 77% for latency and 80% for tokens, each about +/- 6 points
  on 200 days. The level is Holm's first step, which a lone rise must pass: 0.05 over the
  number of metrics tested that day, counting one more for a metric not tested (a pending
  judge, a refused latency). Where no day of that n can alert at all, the report says "cannot
  alert" instead of a number.
- **Output.** A table, `results/online/alerts.json`, and a non-zero exit code on an alert.

## 4. Dashboard (`evals dashboard`, static HTML chosen by Aleena 2026-10-19)

**Built:** `evals dashboard` writes `docs/dashboard/index.html`: one page with no scripts and no
outside requests, rebuilt from the stored runs and the monitor's files. It has one small chart
per alert metric plus fabricated citations. Each chart shows every day's value with its 95%
interval and n. Behind each day is a band showing where a normal day of that n falls 95% of the
time, resampled from the reference days (a reference day's band leaves it out). The charts use
`src/agent_evals/chart.py`'s colours and font. Below the charts are the alert banner, the checked-days table with the judge-trust
flag, and each day's five least-supported answers with the judge's reasons. Latency and tokens
are drawn as geometric means in seconds and tokens. Rates start at zero; the other axes are
labelled at the bottom. It was checked by screenshot at desktop width and at 390 px. A day
checked against another reference, or recorded under another day's number, is left out with a
warning.

- **Recommended:** a static HTML page built into `docs/dashboard/index.html`, which can be
  published with GitHub Pages. It shows the alert family per day with 95% intervals and graded n,
  drawn with `src/agent_evals/chart.py`. It also shows the alert table, the trust flags, and for
  each day the five least-supported answers with the judge's reasons.
- **Alternative:** Streamlit, which is interactive but local only and adds a dependency.

## 5. The demo, every step pre-registered before its day runs

1. **Days 1 to 5** are normal, 100 questions each, and form the reference.
2. **Day 6** is normal and must not alert, as the false-alarm check. **Done 2026-10-20: no
   alert**, with all six family metrics tested (run `docs-20261019-172814-0cbb95`, judged by
   `2cf75fa03e09`). The closest call was lookups and comparisons declined, +5.7 points with p
   0.026, against a level of 0.0083. judge_supported, still pending, was +8.2 points not
   supported. A reviewer's check with the judge in the family gave p 0.07 against 0.0071, so
   it would have stayed quiet too. Its blind spots: uncited +8.1 points, declines +10.9, errors
   +6.0, model calls +0.66, latency +30%, tokens +33%, fabricated +9.4.
3. **Day 7** is a bad deploy: a rollback to prompt v2, with 200 questions (generated as two halves
   of 100 with separate seeds, each half naming 10 different products, so a product may appear in
   both halves; to be built in part 5). The champion also runs
   on the same questions as a shadow, so the paired difference separates the agent from the
   traffic. The expected alert, judge_supported down, is stated with its power, computed from the
   reference's observed rate and graded n.
4. **Day 8** degrades retrieval to one search result, with 200 questions and a shadow. Its
   expected alerts are predicted first, by running that config on the dev split offline.
5. **Days 9 to 11** are blind. Aleena chooses a config for each day: unchanged, a subtle fault, or
   a larger one. She commits only a hash of her choices. The monitor's call is made before the
   choices are revealed and checked against the hash.

## Cost and Aleena's time

- **Money**, recomputed 2026-10-19. The first figure, "about $25 in all", did not match its
  own unit costs, which came to about $34. Measured since: the generator and its checker cost
  $0.62 to $0.64 per 100 questions (days 0 and 1, from the recorded tokens at the published
  prices). Measured on the online runs of days 1 to 5: the agent cost $0.18 to $0.20 per 100
  questions ($0.96 for the five days, with 71% to 73% of its input read from the prompt cache).
  The judge cost $0.45 for day 1's 83 answers, at one call per answer online since the
  responsive check is skipped; days 2 to 5 judged 84 to 89 answers each, so about the same. So
  about $1.30 per 100 questions, against the $1.35 estimated before. Spent on phase 7 so far:
  about $9.30. That is day 0 generated twice, about $1.50; days 1 to 5 generated, about $3.15;
  their online runs, about $3.30; and day 6, about $1.30 (agent $0.19, judge $0.47 for 82
  answers). A few one-off checks come on top. Day sizes,
  settled so the demo and this section agree: days 1 to 6 have 100 questions (day 6 is checked
  against the reference, so it matches it); days 7 to 11 have 200, each with a shadow run of the
  champion. That is about $7 for days 1 to 5, $1.35 for day 6 and about $4 for each 200-question
  day with its shadow, so about $28 in all, plus about $1.50 already spent generating day 0
  twice. Aleena approved about $25; the work stops to ask before passing it, at day 7 at the
  latest. Without shadow runs on the blind days 9 to 11 the total is about $24 (each such day
  saves about $1.40: the champion's second agent run and its judging).
- **Aleena's time:** about 40 minutes, in three short tasks: the pilot of 30 questions (done,
  2026-10-18), 30 judge labels from reference days, and sealing the blind configs. Optional: the
  10-question realism read of generator v2, about 3 minutes (`evals pilot-review 0 --quick`), not
  taken so far.

## Decisions for Aleena (answered 2026-10-19)

1. The online-case grader guards (part 2) and the alert rule (part 3): **approved**.
2. Dashboard: **static HTML**.
3. The budget (about $25 as then estimated) and the three short tasks: **approved**. The
   estimate has since been corrected (see "Cost"), so anything past $25 is asked first.
4. The 10-question realism read of generator v2 (`evals pilot-review 0 --quick`): open, and
   optional.

## How the review of v1 was taken in

| Problem (reviewer) | Change |
|---|---|
| Day-7 power about 0.8 at n of 85 graded, never stated (statistics) | One-sided tests; fault days at 200 questions; power pre-registered from the reference |
| Holm family undefined (statistics) | 7 named metrics; everything else report-only |
| Clustered generation makes the reference noisy (statistics) | Exact quotas per cell; leave-one-day-out check before day 6 |
| Abstention has no bad direction (statistics) | Split by intent; alert on in-scope abstention only |
| Errors vanish from every metric (statistics, design) | Error rate in the family; unconditional supported share reported |
| Latency and cost tests mismatched and fragile (statistics, design) | Log-mean tests; uncached only; tokens rather than dollars |
| Fabricated citations effectively unmonitored (statistics) | A separate safety rule |
| The judge drifts with answer style (statistics, design) | Pinned judge; length and source-naming trust flags; 30 labels before it can alert |
| Day 8 and the reference not pre-registered; an abnormal reference day (statistics) | Both pre-registered; the reference frozen; the demo halts |
| Online cases could be scored correct; `gold` reads would crash (design) | Explicit model and grader changes, with tests; traffic kept outside `suites/` |
| A Jaccard guard misses paraphrases (design) | Guard by source page: no test evidence pages; Jaccard only within a day |
| Title-echo questions are too easy (design) | Symptom-phrased quota; overlap recorded; a 30-question pilot |
| No way to know a decline was right (design) | Outside-snapshot from held-out services; false premise contradicts a stored sentence |
| A v2 rollback is too easy a demo (design) | A blind phase with hash-committed configs, one of them subtle |
| Day-level traffic confounds fault days (design) | Shadow runs of the champion |
| Cached replays break latency; online runs as latest (design) | Cache hits refused for latency; online runs never latest |
| "Service area" sampling undefined (design) | Pages sampled uniformly, a committed area map, area mix reported |

## Code review of part 1 (2026-10-18)

An isolated agent reviewed the code against part 1 and found 16 problems: 7 bugs, 2 mismatches
with this plan and 7 minor ones. All are fixed and covered by `tests/test_simulate.py`. Day 0 was
then regenerated; its first version had never been reviewed.

| Problem | Fix |
|---|---|
| A failed generator reply is cached, so that day could never be rebuilt | Retried under the next sub-salt (sonnet-5-5 rejects a forced tool call) |
| Outside services cycled by slot index: a day used 6 of the 12 | A separate counter: 10 distinct services a day |
| Five held-out services are mentioned in page text | Checked against page text and test questions; eight candidates added |
| Premise keys included code, lists, table cells and text not on the page | Prose paragraphs only, a fact pattern, checked word for word |
| 17 non-evidence pages hold test support sentences | Excluded as sources |
| A duplicate was retried with the identical request | The clash is quoted back; after 6 tries the page is redrawn |
| The area map was rebuilt from the suites, and a third of pages had no area | Committed, covering every page; a gap is an error |
| The pilot judged the source page from its headings and skipped outside questions | Asks about the snapshot for every question, shows the closest passage, enforces the bar |
| Minor: stopwords in the overlap, headings inside code, pilot resume and torn writes, silent overwrites, unnamed services, the judge's strict flag reused | All fixed |

Then the pilot command itself was tested the way Aleena runs it, after the command as first
given to her failed (the day was an option) and a markup crash turned up while fixing it. One
agent drove `evals pilot-review` on a copy of day 0, a second parsed every command in the docs,
and an independent agent reproduced each failure. That confirmed 6 problems in the command, left
4 more unverified, and found 3 outdated commands in old notes. All 10 command problems are fixed,
each with a test: a command-line test in `tests/test_cli_pilot.py` for each, except that the
stratified sample, the passages and the search hits had unit tests only until the session audit
of 2026-10-19 added command-line tests for them too. Two of the old commands are corrected; the
third is left as a superseded record (`docs/judge-validation.md`, section 2). No current
instruction failed to parse.

| Problem | Fix |
|---|---|
| The day was an option, so `evals pilot-review 0` failed | The day is an argument |
| Rich markup dropped "[error]" from questions and crashed on "[/dim]" | Generated and page text printed as plain text |
| An answer typed after a torn last line fused with it and was lost | A newline is written first after a torn line |
| The passage shown often missed the answer on the page | The two best passages by the agent's search ranking, the page link, and "unsure" |
| A partial run's summary read as passing the bar | The bar is decided only when all 30 are reviewed |
| The sample held one outside-snapshot question | Stratified by intent; the bar weighted by traffic share |
| From the wrong folder, a traceback advised rebuilding the snapshot | A plain message with the `cd` to run |
| Minor: a missing day, `--sample` 0 or 4, upper-case answers, q at the note, repeated search hits, "()" in a prompt | Plain messages, a fixed sample, all fixed |

## Pilot and generator v2 (2026-10-18)

**The pilot.** Aleena reviewed the 30 sampled questions of day 0. All 30 were realistic, so the
bar was met. Her answerability answers were then checked against the whole snapshot: one agent
per 6 questions searched and read it, every quote was verified on its page, and a second agent
re-checked each disagreement from scratch (all 10 re-checks held). The check found two things.
The tool did not say what to do with an outside-snapshot question, which has no page to open, so
she answered "unsure" on all 5; she then changed them to "no". More important, the questions
often asked more than the docs say. The whole snapshot fully answered only 6 of the 20 lookups
and comparisons, answered 10 partly and 4 not at all. Generator v1 saw only a page's title and
headings, so it asked about the topic, not about what the text says. Those answers would muddy
the online numbers: a correct decline would count as an in-scope abstention. Her answers, the
check and v1's traffic are kept in `results/online/pilot-v1/`.

**The change, one only (v2).** A lookup or comparison is written from one section of its page,
which the generator sees: at least 300 characters, and not a callout, a list of links or mostly
code. The system prompt adds that the person has not read the docs, so it must not copy their
phrases. A checker call to the same model then judges, from the section alone, whether it answers
every part of the question. A question it calls partial or none is retried with the gap named.
False premises and outside-snapshot questions are unchanged, except that a false premise must ask
nothing its sentence does not settle.

**Checks, fixed before v2's day 0 exists:**

1. Answerability: the pilot's evidence check, the same method, on the 20 lookups and comparisons
   of v2's pilot sample (15 and 5, the same strata). Pass: at least 17 fully answered by the
   snapshot, and at most 1 not answered at all (v1: 6 and 4).
2. Copying: at most 10% of the in-snapshot questions share 6 or more consecutive words with their
   page (v1: 1 of 90).
3. Realism, if Aleena opts in: she reads 10 of v2's questions, 2 per persona. Fewer than 7
   realistic means the prompt is revised.

If check 1 or 2 fails, the prompt is revised and the day regenerated, and every attempt is
reported here.

**Results, v2's day 0.** Check 1 passes: the snapshot fully answers all 20, and the auditor, told
to refute every "full", changed none (43 of 43 quotes verified; `results/online/traffic/
day-00.answerability.json`). The check's first run ended at a usage limit with no verdicts and was
rerun unchanged. Check 2 passes: 2 of the 90 in-snapshot questions copy 6 or more consecutive
words from their page (2%). While generating, the checker rejected 6 drafts that their section
did not fully answer, and each was rewritten. One thing is left to the realism read: some
questions follow the page's own example closely, such as its Ohio-to-Oregon transaction or its
Auto Scaling group of 4, 6 and 12. Check 3 is Aleena's to take or skip:
`uv run evals pilot-review 0 --quick` (10 questions, realism only, at least 7 to pass).

## Session audit (2026-10-19)

Aleena asked for a check that nothing was wrong or missing. A docs auditor checked every number
and claim written in the last day against the result files and the code; a code auditor hunted
for bugs in the pilot tool, generator v2 and prompt caching, replaying day 0 from the call cache
with misses refused (it rebuilds byte for byte) and testing the store migration on a copy of the
results database. Their verifier stopped at a usage limit, so each finding was checked by hand
before it was fixed. Fourteen findings, all fixed or written down:

| Finding | Done |
|---|---|
| The budget's total never matched its unit costs ($34 against "$25") | Recomputed from measured costs ("Cost"); asked before passing $25 |
| Snowball and Snow Family counted as two of a day's 10 different services | One product family a day (generator v2.1); days 1 to 5 regenerated |
| A retry quoted only the last rejected draft, so the generator went back to an earlier one | Every rejection on the page is quoted (v2.1) |
| A failed checker call was counted as a generator failure | Counted apart, as `check_failed` (v2.1) |
| 200-question days cannot be generated, and the plan disagreed on which days had 200 | Two halves of 100 (part 5); day sizes settled in the demo and "Cost" |
| The prompt cache helper could mark an empty block, override a TTL or pass 4 breakpoints | It leaves those alone; tests |
| The generator and checker were said to use the prompt cache; their prompts are too short | Corrected: only the agent and the judge cache |
| Sonnet's 0.05x cache-read price was described but not applied | Set in the judge's pricing, with the published prices |
| No CHANGELOG entries for generator v2 and part 2; part 2 of the plan named no command | Added (CHANGELOG; part 2's "Built" note) |
| "Each with an end-to-end test" overstated the pilot's tests | Reworded; the missing command-line tests added |
| The time budget left out the optional realism read | Added |
| `evals simulate --help` said one call per question | Corrected |

Generator v2.1 changes only retries and the outside services' assignment: a lookup or comparison
is still kept only when the checker finds its section answers every part of it. Day 0, the pilot
day, stays the v2 day it was checked on (its meta records v2); days 1 to 5, the reference, are
v2.1.

## Code review of parts 3 and 4 (2026-10-19)

Two isolated reviewers read the monitor, the judge check and the dashboard at commit 96284ab, one
for the code and one for the statistics. Each worked on its own copy of the results database and
reproduced every finding there. The code review's ten findings:

| Finding | Done |
|---|---|
| A run with no judge grades, or no attempts, read as "no alert" (exit 0); an unjudged reference day could be frozen | Refused: a run must be complete, have attempts, and have every cited answer judged by `2cf75fa03e09`. A family metric with no test leaves the day not cleared (exit 3) |
| The judge check read a lost judge grade as a judge fail, which can never be a false pass, and passed on fewer than 30 answers | Refused: every sampled answer needs a verdict, and the sample must be 6 from each of the 5 days |
| `--freeze` silently replaced the frozen reference, and alerts and the judge check made against the old one stayed in use | Refused without `--refreeze`, which archives them. The judge counts only for the reference it was checked on, and the dashboard leaves out days checked against another |
| A reference day could be checked as a new day, and `--run` could name another day's run | Both refused; the dashboard takes the day from the run |
| On days 7 and 8 the default run would be the champion's shadow, not the deployed run | Runs record whether they are shadows (`online-run --shadow`). The default is the day's one complete served run, and with two, `--run` is required |
| The traffic file was never hash-checked, so a regenerated day changed the trust flags and the dashboard's questions | Recorded with each run and checked. Days 1 to 5 were backfilled: each file matches the hash written when it was generated, and predates its run |
| From the wrong folder, the commands opened a new, empty database and exited 0 | A config file must exist. The monitor and dashboard open the database read-only, so a missing one is an error |
| The leave-one-day-out check never ran with judge_supported in the family | `judge-check score` reruns it with the judge in, once the labels pass. The judge joins only if it stays quiet |
| A NaN latency was Holm-rejected yet showed "ok", and alerts.json got NaN | Latencies that are not finite and positive are skipped; the JSON files refuse NaN |
| The published detectable rise was about 0 when the reference rate is 0 | In `stats/`, so put to Aleena first; approved 2026-10-20 and built (decision 5) |

The frozen reference stands. On the real data, with the fixed code, all five runs pass the new
checks and each is its day's only complete champion run. With judge_supported in the family the
leave-one-day-out check is quiet too: the closest call is day 5's judge_supported, +6.1 points
with p 0.15. The judge still joins the family only through `judge-check score`, after the labels.

The statistics review's eleven findings. It replayed the rule on a copy of the real days and
simulated reference days drawn from one distribution:

| Finding | Done |
|---|---|
| The detectable rise was badly understated for rare rates: uncited answers showed +0.04 points against +7.7 exact. It also ignored the floors: latency showed +17%, though nothing under +25% can alert | Put to Aleena, being in `stats/`; approved 2026-10-20 and built (decision 5) |
| No detectable rise was given for fabricated citations, for judge_supported while pending, or for refused latency | Built with decision 5 |
| The leave-one-day-out check never ran with judge_supported in the family | Fixed with the code review's finding |
| Five days from one distribution still stop the demo at that check about 10% of the time (up to 17% with day-to-day variation), and the plan did not say so | Stated in part 3. Days 1 to 5 passed, so the rule stays as approved |
| A failed reference check could be re-rolled | Refused, after a stop as well; `--refreeze` archives everything first |
| With a metric refused, Holm ran over fewer metrics, but the report still said 6 or 7 | The report gives the number tested; part 3 says how a refused metric is handled |
| Only about 10 of the judge check's 30 answers are ones the judge passed (10 in today's draw), and a false pass can happen only on those, so the bar of 2 is weaker than it looks | Put to Aleena before any label existed; approved 2026-10-20 and built (decision 6) |
| The reported in-scope support also counted false premises | Lookups and comparisons only, as everywhere else |
| The dashboard's band was the pooled mean's interval, which normal days often fall outside (13 of 40 dots) | Each day sits over the range a normal day of its n shows |
| The monitor table showed judge_supported as the unsupported share, and latency and tokens as logs | Rows show their labels; latency and tokens are in seconds and tokens |
| "Moves more than 25%" did not say whether relative or in points | Rejected by the verifier: the plan writes relative changes as % and absolute ones as points, so this was already defined. Part 3 now spells it out anyway |

An independent verifier then checked every finding against the code at 96284ab and a copy of the
database: 20 of the 21 held, and only the last statistics finding was rejected.

## Decisions for Aleena (asked 2026-10-19, both approved 2026-10-20)

5. **The "detectable" column** (`stats/`). **Approved and built** ("Blind spots" in part 3).
   It should be the smallest real rise the monitor
   would catch on 8 days in 10. The formula behind it is a normal shortcut that breaks near 0%,
   and it leaves out the floor an alert also needs. Proposed: compute rates exactly (the
   binomial count the Fisher test needs), count the floor, and give it for every metric,
   including fabricated citations. On today's reference, uncited answers would go from +0
   points to +7.7, errors from +2.7 to +6.0, latency from +17% to about +30%, and fabricated
   citations from none to +8.9. It changes what is reported, never when an alert fires.
6. **The judge check's sample**, before any label exists. **Approved and built** ("Judge check"
   in part 3). Proposed: draw 15 answers the judge
   passed and 15 it failed (3 and 3 a day) in place of 30 at random. Keep the bar (at most 2
   false passes), now applied to 15 judge passes, and also report the answers the judge failed
   that Aleena passes. Same 30 labels, same time.

## Check of decisions 5 and 6 (2026-10-20)

Three isolated reviewers checked commit 8f7e9f5, each on copies of the database and
`results/online/` in a throwaway worktree, and an independent verifier reproduced each finding.
The first rebuilt the blind spots with its own exact arithmetic and matched every real one to
within 6e-15. It found Fisher's p non-increasing in the day's count on 295,200 settings, which
alert_count relies on. The second drove the real labeling screens with scripted answers. The
third checked the docs and ran the next real steps. No finding changes when an alert fires or
any alert decision, on days 1 to 6. Of 13 findings, 11 were confirmed (2 of them duplicates);
all are fixed:

| Finding | Done |
|---|---|
| After a complete score, its result shows the judge's verdicts; a failed check could then be relabeled and rescored, and the first result was overwritten without a trace | The first complete score is final, and a later score only reports it |
| 16 of the 30 sampled answers sit on judge-dev, where `label --show-judge` shows the judge's verdicts before scoring | `--show-judge` is refused on the check's runs |
| A re-judge after the draw that swaps one pass and one fail on a day kept the 3-and-3 count and was scored silently | The sample is saved with a digest of the judge's grades, reasons included (only 20 splits are possible, so a digest of the values alone could be reversed). Labeling and scoring are refused if it changed |
| Any labeler name could switch the judge on; the plan says Aleena's labels | Only her labels count; `judge-check` has no `--labeler` |
| On Windows, a stdin of NUL (a shell with no terminal) reports isatty(), so the labeling screens started and aborted at the first prompt | Interactive screens also require a real console (`label`, `review`, `pilot-review`) |
| A re-recorded reference would carry today's date, after day 6 ran, against the plan's "frozen before any later day ran" | A refreeze of the same runs keeps the freeze date and points to the archived first record |
| A blind spot of "no count can ever alert" showed as "-", like a number not computed | Shown as "cannot alert" |
| The blind spot's wiring in check_day (Holm level, pending and refused metrics, the fabricated rule) had no test: setting the level to 0.5 passed every test | Tested |
| "A random 30 would hold about 10 judge passes": the expectation is 7 or 8; 10 was one draw | Corrected in the plan, the docstring and the CHANGELOG |
| alert_count scanned one exact Fisher test per count, minutes at n of 10,000 | Bisection, about log2(n) tests; the project's days are 80 to 200 |
| Found by the author afterwards: the published dashboard lists each reference day's five least-supported answers with the judge's reasons. These are judge fails, and a sampled one would not be blind | The sample leaves them out, and the dashboard never shows a sampled answer |

Rejected by the verifier: that no test would catch a wrong mean formula (the closed-form tests
fail on a wrong standard error or sign), and that HANDOFF's next step skipped day 6 (part 5
starts with it). The reviewers' simulations put the mean blind spots at 0.76 to 0.82 of days
alerting, as did the author's (see "Blind spots").

## Re-check of the fixes (2026-10-20)

Two more isolated reviewers then tried to defeat the fixed judge check at commit a3f8b84, and
ran the next real steps on copies. They drove the real labeling screens through 30 labels, with
a quit and a resume, and a verifier reproduced each finding. Of 9 findings, 8 held, some of
them the same problem seen twice, and all are fixed:

| Finding | Done |
|---|---|
| A re-record of the same runs (`--freeze --refreeze`) archived the judge check's sample and result. The next score then redrew the same 30 and rescored edited labels, so a failed check could be passed | A re-record of the same runs keeps both in place; only a refreeze on other runs archives them |
| `judge-check score` drew a sample when none was saved, even with Aleena's labels already in | Only `label` draws, and never once her labels exist on the reference runs |
| Running the test suite would have moved the real judge-check sample: one test's refreeze used the real path for it (no sample had been drawn yet, so nothing was lost) | Every test's monitor and dashboard files go to a temporary folder. The run fails if a real `results/online/` file changed, which a probe test confirmed |
| `judge-validate` on a reference run gave the judge's verdicts on her sampled labels before the score (its false negatives are the false passes), so labels could be tuned blind until the check passed | Refused on the check's runs until the check is scored |
| `trace` printed the judge's verdict and reason on a sampled answer | Held back until the check is scored |
| Deleting or editing judge-check.json reopened a final score | The first complete result is also kept in the results database on every reference run; the next score restores the file from it |
| The CHANGELOG claimed the judge's view was refused, while only `label --show-judge` was | Now true of every command that shows it, and worded as such |
| "Points to the archived first one" held only for the first re-record | Corrected: each record points to the one it replaced |

Rejected by the verifier: that the old scan's "minutes at n of 10,000" did not reproduce (it
took over 500 s with a large reference and no floor). The reviewers also confirmed the parts
found sound: the next real steps on copies (frozen_at kept, day 6 clear, a 3-and-3 sample
disjoint from the dashboard, which stays byte-identical), the digest guard, the labeler pin, and
that a real console passes the interactive check while NUL and pipes do not.

## Final check before the draw (2026-10-20)

A last isolated reviewer attacked the judge check at commit 2b0f40d on copies, and a verifier
reproduced each finding. All 5 held. They had one root cause: the check trusted files that
anyone could write, edit or move. So the results database became the only record of the
sample and the score, with the files as its published copies:

| Finding | Done |
|---|---|
| A hand-written judge-check.json counted as a final score before any real one: it lifted the blind, skipped the scoring and put the judge in the family | Only the database's record counts, for every rule |
| The monitor and dashboard trusted an edited judge-check.json over the score in the database | `judge_enabled` reads the database |
| `trace -c configs/judge-v4.yaml` (or judge-v3, judge-v2.1) printed the verdicts it said it held back, since it hid only the config's judge name | It hides every judge's grades on a sampled answer |
| Moving judge-check-sample.json aside lifted the blind, and putting it back resumed the check as if nothing had happened | The sample is kept in the database at the draw, the blind reads it there, and the file is restored from it |
| After a passed check, a re-judge of the sampled answers left the judge in the family | The judge is in the family only while those grades are unchanged |

Everything else tried was stopped. That includes relabeling or `--fresh` after a score,
deleting the result (restored byte for byte), refreezes of the same and other runs before and
after labels and scores, swapped cases and one-character edits to a judge reason, other
labeler names, `--show-judge` in every form, and judge-validate. The next real steps also ran
on a copy: a NUL stdin draws and then refuses, the 3-and-3 sample is disjoint from the
dashboard, and the dashboard rebuilds byte-identical. The worktree's 294 tests passed.

A further isolated reviewer then attacked the database record itself, at commit 355752f.
Every attempt through files was stopped, but it found 4 more problems, all confirmed and
fixed:

| Finding | Done |
|---|---|
| Each run had one slot per record, which a draw or score overwrote, and `judge-check` took its runs from reference.json, a plain file. Drawing and scoring on hand-edited references that shared runs could erase the real score, which could then be made again with changed labels | The check runs only on each day's one complete champion run, and its records are written once |
| After a re-judge of a passed check, the monitor and dashboard said the judge "has not yet passed" | They give the actual reason |
| A hand-written judge-check.json was ignored but left in place, where a commit could publish it | `judge-check` moves a result file with no score in the database to the archive, and says so |
| The no-labels-before-the-draw rule matched the labeler's name exactly | Labels under any name block a draw |

A last reviewer then attacked the write-once records at commit d9974e3. It took about five
hours, with replays and refreezes on copies, and found 6 more problems, all confirmed and
fixed:

| Finding | Done |
|---|---|
| `evals judge` keeps its notes in the manifest under the config's judge name. A config naming its judge `judge_check` could erase a final score, or lock a drawn check out for good, with `--limit 0` and no API call | The store itself refuses to change a written record, whoever asks. `judge` refuses a judge name that names any other record of the run |
| A free cache replay of a reference day gave a new run with the same answers and verdicts, where nothing was held back | The blind holds back the sampled case ids in every run (online ids name their day). `compare` on a judge metric and `judge-probe` are refused on runs holding them |
| `score` checked the five days only when the labels passed, so a refusal hinted at the outcome and let passing labels be redone | It checks the days first, whatever the labels say |
| A second champion run of a reference day (`online-run N`, free from the cache) locked a drawn check out for good | `online-run` refuses a frozen reference day, and a drawn check accepts the runs it was drawn on |
| The sample's five writes were separate, so a draw cut short left some runs unguarded | Written in one transaction, and a partial record is completed on the next call |
| The published copies were not always restored or archived | Every `judge-check` call restores both from the database, or moves a copy of nothing aside |

