# agent-evals

**Measure tool-using LLM agents for accuracy and hallucination, find out *why* they fail, and
improve them only through changes that are proven on held-out data.**

The platform evaluates two agents that engineers use every day:

| Agent | Job | How hallucination is caught |
| --- | --- | --- |
| **AWS docs agent** *(built)* | Answers AWS questions using a dated docs snapshot, citing each claim with a verbatim quote | Every quote is checked against the cited page; invented pages and misquotes are flagged by code, not by an LLM |
| **AWS code agent** *(next)* | Writes boto3 code for AWS tasks | Every API call is checked against botocore's service models; code runs against moto in a no-network sandbox |

> Status: **measurement and the first improvement loop are done for the docs agent.** The
> harness, graders, statistics, results store, an LLM judge validated against a human, and four
> gated improvement iterations (one shipped) work end to end; the charts below summarize them.
> Next: online evals with a simulated user, then the LLM Ops and routing briefs, then the code
> agent (see [Roadmap](#roadmap)).

## Why this is built the way it is

- **Agents plug in through one interface** (`Agent` protocol in `src/agent_evals/types.py`). The
  harness never imports an agent's internals.
- **Cheapest reliable grader first.** Citation checks, keyword checks and abstention checks are
  plain code. An LLM judge is added only for what code can't settle.
- **Every number has a confidence interval.** Bootstrap over cases, paired comparisons between
  runs, exact McNemar test on flipped cases, and τ-bench's pass^k for reliability.
- **Every score is traceable.** Each run stores a manifest: git SHA, agent version hash, model,
  prompt hash, suite hash, docs snapshot fingerprint and package versions.
- **Re-runs are free and identical.** A content-addressed call cache stores every model
  response. Repeated attempts at the same case are salted so they stay separate samples
  ([ADR 0001](docs/adr/0001-call-cache.md)).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). Works natively on Windows, macOS and Linux.

```bash
uv sync                       # install dependencies
cp .env.example .env          # then paste your Claude API key into .env
uv run python scripts/check.py   # lint, format, types, tests, suites (offline; = make check)

uv run evals snapshot         # fetch the AWS docs pages in configs/docs_pages.txt
uv run evals snapshot --check # has the documentation drifted since the snapshot? (monthly in CI)
uv run evals validate suites/docs/smoke.jsonl   # with a snapshot: checks evidence pages and quotes too
uv run evals review suites/docs/<name>.drafts.jsonl --reviewer <you>   # verify drafts one by one
uv run evals split suites/docs/verified.jsonl --dev 130 --test 150 --seed 20261011   # done once; ADR 0006
uv run evals power --cases 150                       # how many cases detect a 10-point change
uv run evals run suites/docs/smoke.jsonl --agent closed-book   # baselines: closed-book, oracle-evidence
uv run evals run suites/docs/smoke.jsonl            # costs a few cents
uv run evals report                                  # summary of the latest run
```

Run with repeated attempts to measure reliability, then compare two runs:

```bash
uv run evals run suites/docs/smoke.jsonl --attempts 3
uv run evals compare <baseline-run-id> <candidate-run-id>
```

Before your first real run, set a monthly spend limit in the Claude Console and fill in
`pricing` in `configs/default.yaml` so the per-run budget cap can work.

## Results so far (updated 2026-10-17)

Docs agent on claude-haiku-5-5 over a 334-page AWS documentation snapshot. Every number has its
95% interval and n; the test split is frozen and was run once per shipped version.

![Gated improvement loop: holdout results for each shipped version and dev results for every
iteration, with 95% intervals](docs/charts/improvement.svg)

![Why answers are not fully supported, by cause, for v3, v4 and v5 on the dev split](docs/charts/causes.svg)

The first chart is the improvement loop. One change shipped (v3, cite every sentence): it lifted
the holdout's fully-supported rate from 11% to 32%, with correctness unchanged. Three later
changes were tried on dev and not kept; the chart gives each one's reason, from the gate or from
the human spot check of the judge. The second chart shows why answers fall short: mostly text
the agent had read but did not quote, which the stricter citation prompts cut by more than half,
although those prompts were not shipped. Both charts are rebuilt from the results store by
`scripts/improvement_chart.py`, and every plotted number is in `docs/charts/improvement.json`.

| Run | Split | Cases | Attempts | correct (95% CI) | fabricated_citation | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| docs agent v3 (after iteration 1) | test | 150 | 1 | **0.980** (0.943 to 0.993) | 0.035 | $0.34 |
| docs agent v2 (baseline) | test | 150 | 1 | 0.987 (0.953 to 0.996) | 0.028 | $0.35 |
| docs agent v3 | dev | 130 | 3 | 0.969 (0.944 to 0.990) | 0.013 | $0.99 |
| docs agent v2 | dev | 130 | 3 | 0.956 (0.921 to 0.985) | 0.021 | $0.97 |
| closed-book baseline (no tools) | dev | 130 | 1 | 0.585 (0.499 to 0.666) | n/a | $0.06 |
| oracle baseline (gold pages given) | dev | 130 | 1 | 0.938 (0.883 to 0.968) | 0.000 | $0.07 |

Rates in this table are per run. The CHANGELOG pairs the cases two runs share, so a figure can
differ slightly: v3's test fabricated_citation is 0.035 over its 143 cited cases and 0.028 over
the 142 it shares with v2.

- Retrieval is worth 37 points: closed-book minus the docs agent (v2) on the same dev cases is
  -0.372 (95% CI -0.462 to -0.282, McNemar p = 3e-12).
- Retrieval is not the bottleneck: the oracle, handed the right pages, scores 0.018 below the
  docs agent (95% CI -0.064 to +0.028, no detectable change).
- Reliability on dev with 3 attempts (v2): pass^3 0.938, pass@3 0.977, **flip rate 0.038**
  (0.017 to 0.087). It first read 0.100; most of that was the keyword grader (next point). A
  change between two prompt versions smaller than that band is noise.
- Dev is the harder split by design (it holds the paraphrase groups and the held cases), which is
  why its score is below test's. Dev correctness was regraded after iteration 1 widened the
  keyword lists of 20 dev cases, and again on 2026-10-17 after 10 more, both approved by Aleena
  (gold answers unchanged; test cases untouched). The second regrade covered all six dev runs,
  including the two baselines the first had skipped, so every dev number above rests on the same
  keyword lists.
- LLM judge (Sonnet 5.5, run after the fact on the stored answers), **validated against 65 hand
  labels made blind**: on "fully supported" (every claim in the answer is in the quotes it cites)
  the judge and the human labeler agree on all 65, kappa 1.00, on 36 of 40 fresh answers in a
  second check (kappa 0.80, 95% CI 0.58 to 0.95), and on 38 of 40 more in a third, labeled under
  the revised rubric (kappa 0.86, 0.61 to 1.00). That is this project's claim-level
  hallucination rate. **Iteration 1** (one prompt change: quote every sentence, cite table
  headers) moved it on the frozen test split from **10.6% to 31.8%** of answers fully supported
  (+21 points, 95% CI +14 to +30, p = 8e-7) with correctness unchanged (0.987 to 0.980, interval
  spans zero). The gate, the dev numbers, the one cost (run-to-run consistency dipped from 97%
  to 93%) and the grader fix it exposed are in [CHANGELOG.md](CHANGELOG.md) and ADR 0008.
  Iterations 2 and 3 were rejected by the gate and are recorded there too. Iteration 3's prompt
  doubled the dev fully-supported rate (0.31 to 0.60), but it abstained on more answerable
  questions and correctness slipped, so v3 stays. Iteration 4 (v4 plus an abstention guard)
  passed the dev gate, but a pre-registered human spot check found the judge too lenient on its
  shorter answers. Two attempts to repair the judge each missed their rule by one answer, so it
  is not shipped. By the labeler's own 90 labels its answers are fully supported 60% of the time,
  against 34% for v3, on different samples (a random 90 for v5, the first 80 in case-id order
  for v3), so not a paired comparison.
  "Core supported" (just the claims that answer the question; 89.3% pass) is reported as
  advisory: kappa 0.37 on the 65, 0.79 on the 40 fresh answers but with only 3 failures among
  them, so the interval spans 0 to 1. Two candidate judges with revised rubrics were each tested
  on 40 fresh labels under a rule fixed in advance, and neither replaced it: the second tied on
  the labels and missed one planted error, on a mutant later found to plant none. "Responsive"
  (0.997 pass) is validated on 61 closed-book answers at kappa 0.68 to 0.84. Planted-error
  mutants were caught 100% of the time on the first dev run and 55 of 56 on the current
  champion's run (the miss planted no error); verdict stability under re-sampling is 84% and 95%
  on those runs. Everything, including the rounds that failed:
  [docs/judge-validation.md](docs/judge-validation.md).
- What broke: all 32 failing dev attempts were read. None states a wrong fact. 13 are keyword
  grader misses, 12 are the agent answering correctly while marking itself abstained, 6 are case
  defects. Details and two traces per category in [docs/failure-taxonomy.md](docs/failure-taxonomy.md).

## Metrics so far

| Metric | Meaning | Better |
| --- | --- | --- |
| `correct` | Required facts present (answerable), or abstained (unanswerable) | higher |
| `fabricated_citation` | Any citation to a page not in the snapshot, or a quote not on the page | lower |
| `citation_valid_rate` | Share of citations that check out | higher |
| `uncited_answer` | Answered without any citation | lower |
| `wrong_abstention` / `missed_abstention` | Abstained when it shouldn't have / answered when it should have abstained | lower |
| `evidence_recall` | Share of gold pages the agent actually read or cited | higher |
| `evidence_precision` | Share of the pages the agent read that were gold pages (omitted when it read nothing) | higher |
| `unsupported_entity` | Answer states a number or URL found in neither the question nor any page the agent read or cited | lower |
| `repeated_calls` | Identical tool calls (same tool, same input) within one trajectory: loop detection | lower |
| `model_calls`, `tool_errors`, `rejected_submissions` | Efficiency and recovery signals from the trajectory | lower |
| `judge_supported` | LLM judge, validated: every claim in the answer is backed by its cited quotes (claim-level hallucination, inverted) | higher |
| `judge_core_supported` | LLM judge, advisory: the claims that answer the question are backed by the cited quotes | higher |
| `judge_responsive` | LLM judge: the answer addresses the question as asked and agrees with the gold on substance | higher |
| pass^k | Chance that all k attempts at a case pass; what a user who asks once gets | higher |
| pass@k | Chance that at least one of k attempts passes; the gap to pass^k is reliability lost | higher |
| flip rate | Share of cases whose repeated attempts disagree: the noise floor for any comparison | lower |

In the brief's words: `uncited_answer`, `wrong_abstention` and `missed_abstention` are this agent's
policy violations. A fabricated-action rate does not apply, because the agent only reads.
`unsupported_entity` is deliberately not called "fabricated": a converted figure ("15 minutes"
for a page that says "900 seconds") is flagged too, and the LLM judge settles those (ADR 0007).
The closed-book baseline scores near 100% on it by construction, which is what a baseline is for.

## Project layout

```text
src/agent_evals/
  types.py              Case, AgentResult, Step, Grade, Agent and Grader protocols
  config.py             typed YAML config
  corpus.py             documentation snapshot: BM25 search, page reads, quote checks
  tool_markup.py        detects tool-call syntax leaked into an answer
  harness/              call cache, cached Claude client, runner, run manifests
  agents/docs/          snapshot builder, prompts, tool-use loop
  graders/              deterministic graders (answer, citations, output format, trajectory)
  stats/                bootstrap CIs, paired differences, pass^k, McNemar
  store/                DuckDB results store
  report.py             run summaries and paired comparisons
  cli.py                `evals` command
suites/docs/            eval cases, one JSON object per line
configs/                default config and the docs page list
docs/adr/               architecture decision records
tests/                  offline tests using a scripted fake model
```

## Roadmap

1. **Foundations** *(done)*: agent protocol, docs agent, call cache, deterministic graders, store, CLI, CI.
2. **Measurement**: 250 docs cases (dev/test splits), claim extraction, NLI cross-check, LLM judge validated against 100 hand labels and mutation tests.
3. **Code agent**: boto3 agent, botocore API-model checker, Docker + moto sandbox with no network.
4. **Baselines and reporting**: closed-book and oracle-evidence baselines, Streamlit dashboard, PR comment bot.
5. **Learning loops**: failure memory, reflective prompt optimization benchmarked against DSPy GEPA, champion-vs-challenger release gate.
6. **Red team and write-up**: prompt-injection suites, ablations, technical report.

## Limitations

- The AWS docs snapshot is a point-in-time copy; answers reflect the snapshot date, not today.
- Keyword grading (`must_include`) can't judge nuance; it is a first pass before the LLM judge.
- The snapshot is not committed. Check the AWS documentation license before redistributing it.
