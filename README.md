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
uv run evals review suites/docs/ai-ml.drafts.jsonl --reviewer <you>   # verify drafts one by one
uv run evals split suites/docs/verified.jsonl --dev 100 --test 150 --seed 1   # stratified dev/test
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

## Metrics so far

| Metric | Meaning | Better |
| --- | --- | --- |
| `correct` | Required facts present (answerable), or abstained (unanswerable) | higher |
| `fabricated_citation` | Any citation to a page not in the snapshot, or a quote not on the page | lower |
| `citation_valid_rate` | Share of citations that check out | higher |
| `uncited_answer` | Answered without any citation | lower |
| `wrong_abstention` / `missed_abstention` | Abstained when it shouldn't have / answered when it should have abstained | lower |
| `evidence_recall` | Share of gold pages the agent actually read or cited | higher |
| `model_calls`, `tool_errors` | Efficiency and recovery signals from the trajectory | lower |
| pass^k | Chance that all k attempts at a case pass | higher |

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
