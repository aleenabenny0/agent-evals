# Technical report: measuring, improving and operating a tool-using LLM agent

*agent-evals, as of 2026-11-03. Every figure below comes from the project's records, with links to
where each was measured.*

## In one paragraph

This project builds an AWS documentation agent on Claude Haiku 5.5 and, around it, the machinery to
answer three questions honestly: how often is it right, how often does it say things its sources do
not support, and did a change actually make it better? Correctness and citations are checked by
code. An LLM judge handles only what code cannot, and it counts only after it agrees with blind
human labels. A change ships only if it wins on held-out questions under rules written down before
the run. The agent was then monitored on simulated traffic, run as a service with an automatic
rollback, and compared with larger models. Failed attempts stay on the record beside the
successful ones.

## Results at a glance

| Question | Answer | Where |
|---|---|---|
| Is the agent right? | 0.980 correct on 150 frozen test questions (95% interval 0.943 to 0.993) | [README](../README.md#results-so-far-updated-2026-10-28) |
| Do retrieved documents matter? | Yes, by 38 points: without tools the same model scores 0.585 on dev | README |
| Does it cite what it says? | One prompt change raised fully supported answers on test from 10.6% to 31.8% (+21 points, interval +14 to +30), correctness unchanged | [CHANGELOG](../CHANGELOG.md) |
| Are changes tested before shipping? | Four were gated. One shipped, two failed the gate, one failed a human spot check of the judge | CHANGELOG, [ADR 0008](adr/0008-gated-improvement-loop.md) |
| Would monitoring catch a regression? | All three planted retrieval faults alerted, two of them in a blind test sealed in advance, and the unchanged days stayed quiet | [phase 7 plan](plans/phase-7-online-evals.md) |
| Can a bad release undo itself? | In a drill, the service removed a bad prompt by itself in 5.2 minutes, after 20 bad answers | [postmortem](postmortems/2026-11-02-release-drills.md) |
| Is a bigger model worth it? | Not for correctness: Haiku matched Sonnet and Opus at 3.5% of Opus's cost. For fully supported answers, Sonnet beat Haiku by about 17 points at 14 times the cost | [phase 9 plan](plans/phase-9-routing.md) |

## 1. The problem

An agent that answers from documents can fail in two ways. It can be wrong, or it can be right while
saying things its sources do not support, which is hallucination even when the facts happen to be
true. Most agent demos show neither rate. Most improvement claims rest on a handful of examples, so
noise looks like progress. This project treats the agent as something to be measured, like a
service with an error rate, and treats each change as an experiment that needs to beat the noise.

## 2. The system

- **The agent.** It answers AWS questions from a 334-page documentation snapshot. It searches,
  reads pages, and submits an answer in which each claim cites a verbatim quote. It may abstain
  when the documents do not say.
- **The harness.** It runs any agent through one interface, stores every attempt with its full
  trajectory in DuckDB, and records a manifest for each run: git SHA, agent version, model, prompt
  hash, suite hash and snapshot fingerprint.
- **A call cache.** Every model response is stored under a hash of its request. A rerun is free and
  identical. Repeated attempts at the same question are salted so they stay separate samples
  ([ADR 0001](adr/0001-call-cache.md)).
- **Graders, cheapest first.** Code checks that every cited page exists and every quote appears on
  it, that required facts are present, that abstentions are correct, and that the output matches a
  strict schema. An LLM judge (Sonnet 5.5) decides only whether every claim is backed by its quotes.
- **Statistics.** Every rate has a 95% interval: a Wilson interval for proportions, and a bootstrap
  over questions for means. Every comparison is paired, so question difficulty cancels out, and an
  exact McNemar test runs on the questions where two runs disagree
  ([ADR 0004](adr/0004-confidence-intervals.md)).
- **Engineering.** Python 3.12, Pydantic, typer, mypy in strict mode, 627 offline tests, and CI on
  every push. Twelve architecture decision records explain the choices.

## 3. Measurement

- **The questions.** 280 cases, each verified by hand against the snapshot. They were frozen into a
  dev split of 130 and a test split of 150 with a fixed seed
  ([ADR 0006](adr/0006-case-provenance-and-splits.md)). The five types are lookups (146),
  changed defaults (46), multi-step questions (29), false premises (31), and unanswerable questions
  (28), which test whether the agent abstains.
- **Baselines.** Closed-book, the same model with no tools, scores 0.585 on dev: retrieval is worth
  38 points (paired difference -0.377, interval -0.467 to -0.287, p = 2e-12). An oracle handed the
  right pages scores no better than the agent itself (-0.015, interval -0.062 to +0.028), so finding
  pages is not the bottleneck.
- **Noise.** With 3 attempts per question, 3.1% of dev questions flip between pass and fail on their
  own under the v2 prompt, and 4.6% under v3. A difference smaller than that is noise.
- **The judge, validated before use.** On 65 blind hand labels the judge agreed on all 65 (Cohen's
  kappa 1.00, a measure of agreement beyond chance). It agreed on 36 of 40 and 38 of 40 in two later
  fresh checks. It caught 57 of 58 planted errors.
- **Reading the failures.** All 32 failing dev attempts were read by hand. None stated a wrong fact.
  13 were the keyword grader missing a correct answer, 12 were correct answers the agent had marked
  as abstentions, and 6 were defects in the cases. So the instrument was fixed before the agent:
  keyword lists were widened and one case was corrected, each change approved before regrading
  ([failure taxonomy](failure-taxonomy.md)).

## 4. The improvement loop

Each change is one thing: a prompt version, a check or a rule. It runs as a challenger against the
current champion, on the same dev questions with 3 attempts each. It passes only if the target metric
improves with an interval that excludes zero, no monitored metric gets worse, and no question type or
service area regresses. A change that passes runs once on the frozen test split. Both splits go in
the CHANGELOG, passes and rejections alike ([ADR 0008](adr/0008-gated-improvement-loop.md)).

| Iteration | Change | Outcome |
|---|---|---|
| 1 | Cite every sentence, and cite table headers | **Shipped.** Fully supported on test 10.6% to 31.8%, correctness unchanged |
| 2 | Reject answers with numbers no quote contains | Failed the gate. Unsupported numbers went to zero, but the target did not move and rejected submissions rose |
| 3 | Check each sentence, quote whole units | Failed the gate. Answers got shorter, abstained more on answerable questions, and correctness slipped |
| 4 | Iteration 3 plus a guard against needless abstention | Passed the gate, then failed a pre-registered human spot check: the judge was too lenient with its shorter answers |

## 5. Monitoring on simulated traffic

A simulated user, five LLM personas, writes each day's questions from snapshot pages. An alert
compares each day with five frozen reference days, on six code-graded signals tested together with
a correction for multiple comparisons. On some days the champion answers the same questions as a
shadow, so a changed configuration can be told apart from unusual traffic.

- **Planted faults:** three days ran with retrieval cut back, and all three alerted.
- **A blind test:** before days 9 to 11, the author sealed a choice for each day (unchanged, subtle
  fault or larger fault) as a hash. Every verdict was committed before the reveal. The result was
  caught, quiet, caught.
- **A predicted miss:** a rollback to an older prompt made answers less supported, which no
  code-graded signal sees. It was pre-registered as a miss, and was one. The judge could have seen
  it, but it had failed a blind check on online answers (4 false passes in 15, against a bar of 2),
  so it is kept out of the alert.
- **A protocol slip:** one day ran three times by mistake. The seal, not a person, decided which run
  counted, and the tool now checks the seal before it spends anything. The phase cost about $29.

## 6. Operating it as a service

The agent runs behind a FastAPI service under Docker Compose, with OpenTelemetry traces in Jaeger and
Prometheus metrics in Grafana ([phase 8 plan](plans/phase-8-llm-ops.md)).

- **A prompt registry** switches versions without a deploy, and every change is logged.
- **A gateway** adds timeouts, retries and a circuit breaker per provider, so an outage costs one
  timeout rather than one per request.
- **A gate receipt in CI.** CI holds no API key, so it cannot measure the agent. Instead it fails
  any change to the agent's behavior that has no committed record of a passed gate.
- **A canary with automatic rollback.** A new version gets 20% of traffic and is compared with the
  champion on code-graded bad answers, at three looks with a one-sided Fisher exact test. Each look
  is held to 0.33%, so the overall false-alarm rate stays under 1%.
- **Drills, with pass marks committed first.** The first drill failed: its planted fault did no
  measurable harm, so nothing fired. A rerun with a fault measured first rolled back by itself in
  5.2 minutes. A harmless change stayed quiet. The [postmortem](postmortems/2026-11-02-release-drills.md)
  is blameless and keeps the failure.
- **A blind spot, closed.** A pilot showed that a prompt telling the agent not to read pages gave
  cited answers from search snippets, which the rule's signal could not see. The signal now also
  counts an answer that read no page. In a real drill the rule removed that prompt at its first
  look, where the old signal on the same answers would have stayed quiet
  ([drill 1c](plans/phase-8-drill-1c.md)).
- **A load test.** A real answer takes about 11 seconds and 5 model calls, so 8 slots serve about
  0.6 answers a second. Refusals past capacity followed the Erlang B formula from queueing theory.
  Service objectives were set from the measurements ([runbook](runbook.md)).
- **Not run:** failover to a second provider is built and tested offline only. The project has no
  budget for a second paid provider.

## 7. Is a bigger model worth it?

The same agent ran on Sonnet 5.5 and Opus 5.5. Five routers were fitted on dev, each picking a model
per question, and the chosen ones were committed before the test split was read once
([phase 9 plan](plans/phase-9-routing.md), [ADR 0012](adr/0012-routing-between-models.md)).

| Test split | Correct | Cost per answer, list price |
|---|---|---|
| Haiku 5.5 | 0.980 | $0.0023 |
| Sonnet 5.5 | 0.953 | $0.033 |
| Opus 5.5 | 0.960 | $0.069 |
| Haiku, escalating to Sonnet when it abstains | 0.987 | $0.0078 |

- **On correctness, the cheapest model is enough.** The cascade matched Opus at 11% of its cost,
  but it was no better than Haiku alone, so the service keeps serving Haiku.
- **On support, larger models help.** Sonnet gave answers that were correct and fully supported
  about 17 points more often than Haiku (interval +7 to +26), at 14 times the cost. This comparison
  was made after the fact, and is labeled as such.
- **The judge set the limit again.** It passed a blind spot check on Sonnet's answers and failed one
  on Opus's (3 false passes in 30, against a bar of 2). So every support claim that rested on Opus,
  including both routing claims, was withdrawn.

## 8. What the record shows

- **The judge is the weakest instrument, and blind labels are what catch it.** It was validated on
  Haiku's answers to the offline questions. Blind checks then found it too lenient three times: on
  shorter answers from a new prompt, on online answers, and on Opus's answers. Each time the rule
  fixed in advance withheld its numbers. A judge's validation holds only for answers like the ones
  it was validated on.
- **Most failures were measurement failures.** Of 32 failing attempts, none stated a wrong fact.
  So graders and cases were fixed, each fix approved before any regrade. The one gold answer that
  was edited was approved first and is disclosed.
- **Rules written in advance keep results honest.** Pass marks, sealed choices and frozen router
  choices meant a failure could not quietly be reinterpreted. A gate rule that was changed after
  being read is disclosed beside its result.
- **Sample size limits every claim.** 150 test questions cannot show small differences. The canary
  rule catches a broken prompt within about 100 answers, but often misses a change of 10 points. The
  plans say this beside each result.
- **A rehearsal cannot test its own prediction.** The failed drill had passed its free rehearsal,
  because the fake model had been told what the faulty prompt would do.

## 9. Limitations

- One domain: AWS documentation, from a dated snapshot.
- Keyword grading cannot judge nuance. The judge covers support, not every kind of error.
- The online questions are simulated, and no human read the scored days' questions for realism.
- The prompt was tuned on Haiku over four iterations, which may favor Haiku in the model comparison.
- Failover has not run against a real second provider.

## 10. Where to look

- [README](../README.md): every result table, with links.
- [CHANGELOG](../CHANGELOG.md): every gated change, shipped or not.
- [docs/plans/](plans/): each phase's plan, its pre-registered rules and its result.
- [docs/adr/](adr/): twelve decision records.
- [docs/judge-validation.md](judge-validation.md): every judge check, including the failed rounds.
- `src/agent_evals/`: the harness, graders, statistics, service and routers. `tests/` holds the
  offline tests.
