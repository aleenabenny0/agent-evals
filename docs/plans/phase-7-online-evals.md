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
  questions, so 70/10/10/10. If the leakage guard drops a question, its cell is regenerated with a
  sub-salt until full, so every day has the same N and mix.
- **Personas:** a new developer; an on-call engineer, terse, with error messages and typos; a
  security reviewer; a cost-conscious manager; a non-native English speaker. At least one in four
  how-to questions is phrased as a symptom ("I get AccessDenied when ..."), not in the docs' own
  words.
- **Sources.** Each in-snapshot question comes from one source page, drawn uniformly from the
  pages. Every gold evidence page of a test case is excluded, so the test split stays clean even
  if online failures later feed an iteration. The page-to-area map is committed, and the area mix
  is reported per day.
- **What the generator sees.** `claude-sonnet-5-5` sees the page's title and headings. For a
  false premise it also sees one sampled sentence, which it must contradict; that sentence is
  stored as the question's minimal key (`gold.support`). Outside-snapshot questions name a service
  from `suites.held_out_services`, which `evals validate` guarantees is absent, so declining them
  is known to be right.
- **Recorded per question:** source page, persona, intent, and word overlap with the page's title
  and headings. Within a day, near-duplicates (word-set Jaccard 0.6 or more) are dropped.
- **Storage.** `results/online/traffic/day-NN.jsonl`, outside `suites/`, so snapshot checks and
  evidence indexes never load it. Generation is cached by day, so a day can be rebuilt exactly.
- **Pilot before day 1.** Aleena reads 30 generated questions and marks each realistic or not, and
  answerable from the snapshot or not. The rates are published. If fewer than two thirds are
  realistic, the prompts are revised before day 1.

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
  shadows. Each 100 questions costs about $1.20: $0.05 for the generator, $0.25 for the agent and
  $0.90 for the judge.
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
