# agent-evals

**Measure tool-using LLM agents for accuracy and hallucination, find out *why* they fail, and
improve them only through changes that are proven on held-out data.**

The platform evaluates two agents that engineers use every day:

| Agent | Job | How hallucination is caught |
| --- | --- | --- |
| **AWS docs agent** *(built)* | Answers AWS questions using a dated docs snapshot, citing each claim with a verbatim quote | Every quote is checked against the cited page; invented pages and misquotes are flagged by code, not by an LLM |
| **AWS code agent** *(next)* | Writes boto3 code for AWS tasks | Every API call is checked against botocore's service models; code runs against moto in a no-network sandbox |

> Status: **Foundations phase.** The harness, docs agent, deterministic graders, statistics and
> results store work end to end. The LLM judge, code agent and learning loops are next (see
> [Roadmap](#roadmap)).

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

## Results so far (2026-10-11)

Docs agent on claude-haiku-5-5 over a 334-page AWS documentation snapshot. Every number has its
95% interval and n; the test split is frozen and was run once.

| Run | Split | Cases | Attempts | correct (95% CI) | fabricated_citation | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| docs agent | test | 150 | 1 | **0.987** (0.953 to 0.996) | 0.028 | $0.35 |
| docs agent | dev | 130 | 3 | 0.918 (0.874 to 0.956) | 0.021 | $0.97 |
| closed-book baseline (no tools) | dev | 130 | 1 | 0.546 (0.460 to 0.629) | n/a | $0.06 |
| oracle baseline (gold pages given) | dev | 130 | 1 | 0.923 (0.864 to 0.958) | 0.000 | $0.07 |

- Retrieval is worth 37 points: docs agent minus closed-book on the same dev cases is -0.372 for
  the baseline (95% CI -0.462 to -0.282, McNemar p = 3e-12).
- Retrieval is not the bottleneck: the oracle, handed the right pages, is +0.005 over the docs
  agent (95% CI -0.046 to +0.054, no detectable change).
- Reliability on dev with 3 attempts: pass^3 0.862, pass@3 0.962, **flip rate 0.100** (0.059 to
  0.164). A change between two prompt versions smaller than that band is noise.
- Dev is the harder split by design (it holds the paraphrase groups and the held cases), which is
  why its score is below test's.
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
