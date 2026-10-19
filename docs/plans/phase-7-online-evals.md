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
  reason. 13 remain, and a day's 10 outside questions name 10 different services.
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

## 2. Online runs (grader guards, to agree first)

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

## 3. Monitor and alert (`evals monitor`), to agree first

- **Reference.** Days 1 to 5, frozen. Before any later day is judged, a leave-one-day-out check
  runs: each reference day is tested against the other four with the full rule. It must raise no
  alert, or the reference is too noisy and the demo stops.
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
- **Reported but never alerting:** judge_supported over all in-scope questions, counting
  abstained, uncited and errored answers as not supported, which shows the denominator effect;
  answering an outside-snapshot question (the declines are known to be right); false-premise
  outcomes per intent; judge_core_supported, which is advisory; median answer length; and the
  share of answers naming their source page.
- **Judge trust.** If median answer length or the source-naming share moves more than 25% from
  the reference, that day's judge_supported is marked untrusted. An alert still stands, but a
  non-alert does not clear the day. Before judge_supported may alert at all, Aleena labels 30
  answers from reference days blind. The judge must pass at most 2 she fails, the same bar as
  iteration 4's spot check.
- **Mechanics.** Permutations use at least 20,000 draws, with p = (k+1)/(R+1). Any day with cache
  hits is refused for latency. The minimum detectable effect is published for every metric, at
  the day's n against the reference n.
- **Output.** A table, `results/online/alerts.json`, and a non-zero exit code on an alert.

## 4. Dashboard (`evals dashboard`)

- **Recommended:** a static HTML page built into `docs/dashboard/index.html`, which can be
  published with GitHub Pages. It shows the alert family per day with 95% intervals and graded n,
  drawn with `src/agent_evals/chart.py`. It also shows the alert table, the trust flags, and for
  each day the five least-supported answers with the judge's reasons.
- **Alternative:** Streamlit, which is interactive but local only and adds a dependency.

## 5. The demo, every step pre-registered before its day runs

1. **Days 1 to 5** are normal, 100 questions each, and form the reference.
2. **Day 6** is normal and must not alert, as the false-alarm check.
3. **Day 7** is a bad deploy: a rollback to prompt v2, with 200 questions. The champion also runs
   on the same questions as a shadow, so the paired difference separates the agent from the
   traffic. The expected alert, judge_supported down, is stated with its power, computed from the
   reference's observed rate and graded n.
4. **Day 8** degrades retrieval to one search result, with 200 questions and a shadow. Its
   expected alerts are predicted first, by running that config on the dev split offline.
5. **Days 9 to 11** are blind. Aleena chooses a config for each day: unchanged, a subtle fault, or
   a larger one. She commits only a hash of her choices. The monitor's call is made before the
   choices are revealed and checked against the hash.

## Cost and Aleena's time

- **Money:** about $25 in all. That covers 5 days of 100 questions and 6 days of 200 with
  shadows. Each 100 questions costs about $1.65: about $0.50 for the generator and its checker,
  $0.25 for the agent and $0.90 for the judge.
- **Aleena's time:** about 40 minutes, in three short tasks: the pilot of 30 questions, 30 judge
  labels from reference days, and sealing the blind configs.

## Decisions for Aleena

1. Approve the online-case grader guards (part 2) and the alert rule (part 3).
2. Dashboard: static HTML (recommended) or Streamlit.
3. The budget and the three short tasks.

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
each with an end-to-end test in `tests/test_cli_pilot.py`. Two of the old commands are
corrected; the third is left as a superseded record (`docs/judge-validation.md`, section 2). No
current instruction failed to parse.

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
