# Project handoff: context for Claude Code and Codex

This file carries over the planning conversation that produced this repo. Read it fully at
the start of a session. Keep the "Current status" and "Next tasks" sections up to date as
work lands.

## About the author and how to work with her

- Aleena: recent CS graduate (AI concentration, security minor), Python-first. This project is
  a GitHub and resume centerpiece for senior-leaning SWE/AI roles, including public-sector AI.
- She must be able to explain every line in an interview. Explain what you are about to do and
  why, in plain language, before doing it. In `graders/` and `stats/`, agree on the approach
  before writing code.
- She is new to some of this tooling (boto3, uv, eval statistics). Give one step at a time,
  say what success looks like, and define jargon the first time you use it.
- Machine: Windows, PowerShell, VS Code. No `make` and no WSL yet. Project path:
  `C:\Users\Vineetha\projects\agent-evals\agent-evals`.
- Always ask before running anything that costs money (`uv run evals run`, `make smoke`).

## What the project is

A Python platform that evaluates tool-using LLM agents, attributes why they fail, and improves
them only through changes proven on held-out data. Two agents engineers use daily:

1. **AWS docs agent** (built): answers AWS questions from a dated docs snapshot, citing each
   claim with a verbatim quote. Hallucination is caught by code: cited page must exist in the
   snapshot and the quote must appear on it.
2. **AWS code agent** (phase 3): writes boto3 code for AWS tasks. Every API call is checked
   against botocore's service models (no judge needed); code runs against moto (in-memory
   AWS mock) in a no-network Docker sandbox; hidden tests assert final state.

Agent model: Claude Haiku 5.5 (`claude-haiku-5-5`). Judge (phase 2): Claude Sonnet 5.5.
Prompt optimizer (phase 5): Claude Opus 5.5.

### Design principles

1. Every number is traceable (run manifest: git SHA, agent version hash, suite hash, snapshot
   fingerprint, package versions).
2. Cheapest reliable grader first: code, then a local NLI cross-check, then an LLM judge, then
   a human review queue for disagreements.
3. Held-out test data never feeds learning. Lessons and prompt changes come only from `dev`.
4. No change ships without a measured improvement (paired comparison, CI excludes zero).
5. Every failure gets one cause: tool use, retrieval, reasoning, generation, or policy.
6. One command reproduces results (call cache makes re-runs free and identical).
7. Significant decisions get an ADR in `docs/adr/`.

### Metrics (target set)

Outcome: accuracy, hallucination rate (claims unsupported by retrieved evidence), fabrication
rate (fake citations / invented API calls), changed-default correctness, abstention
precision/recall, code safety. Trajectory: tool-call validity, step efficiency, error recovery.
Retrieval: evidence recall@k. Reliability: pass^k (tau-bench), calibration error. Security:
prompt-injection success rate. Cost: cost per correct answer, p50/p95 latency.

### Continuous learning design (phase 5)

- **Failure memory** (every run): each failed dev case gets a short lesson written by a model;
  the lesson is kept only if re-running the case with it passes and it doesn't break similar
  cases; top-3 relevant lessons are retrieved at answer time; unhelpful lessons are pruned.
- **Reflective prompt optimization** (on demand): cluster dev failures, have Opus propose
  candidate prompts, race them on dev; benchmark against DSPy GEPA.
- **Human review queue**: judge/NLI disagreements and low-confidence verdicts go to Aleena;
  her labels become judge-validation data and new dev cases.
- **Champion vs challenger release gate**: a release bundle (prompt, lessons, model, tool
  specs) is promoted only if test accuracy improves with a CI excluding zero, hallucination,
  fabrication and injection rates don't rise, pass^3 doesn't drop, and all regression cases
  still pass. Promotion is a pointer change, so rollback is instant.

### Judge validation (phase 2)

Before LLM-judge scores count: agreement with ~100 hand labels (Cohen's kappa >= 0.7);
mutation tests (plant errors in correct answers; judge must catch >= 90%); verbosity and order
bias probes; stability across re-judging. Pin judge model and prompt version.

### Roadmap

1. Foundations (done): harness, docs agent, deterministic graders, stats, store, CLI, CI.
2. Measurement: ~250 docs cases (100 dev / 150 test, sized by power analysis to detect a
   10-point change), paraphrase robustness suite, claim extraction, NLI cross-check, validated
   LLM judge. Gate: judge passes validation.
3. Code agent: boto3 agent, botocore API-model checker, Docker + moto sandbox (needs WSL2 +
   Docker Desktop on this machine). Gate: runs through the harness with no harness changes.
4. Baselines and reporting: closed-book and oracle-evidence baselines, Streamlit dashboard,
   PR comment bot. (MVP line: a complete, publishable project.)
5. Learning loops (above).
6. Red team (prompt injection planted in doc pages and tasks) and technical report.

## Current status (2026-10-10)

- Repo builds and all checks pass on Windows: `uv sync`, `uv run pytest` -> 56 passed.
- Docs snapshot built: 8 pages in `data/snapshots/aws-docs` (S3 x5, Lambda, DynamoDB, IAM).
  Quote matching verified on a real page.
- First real smoke run: `docs-20261010-174408-ae0812` (agent `3960330eeb86`). 7/7 correct,
  0 fabricated citations, correct abstention on the out-of-snapshot Kinesis question, evidence
  recall 1.0, ~3.3 model calls per case, 87k input / 6.7k output tokens total. `strict: true`
  tool schemas were accepted by the API for Haiku 5.5.
- Finding (fixed, see next bullet): on `docs-smoke-003` the model wrote its citations inside
  the `answer` string (the answer ends with `</answer>` then `<parameter name="citations">[...]`)
  and submitted an empty `citations` list, so it was graded `uncited_answer`.
- Leaked-citations fix landed (commits `f081f42`, `08ca110`). After the schema check,
  `submission_problems` in the docs agent rejects a non-abstained answer with no citations, or
  an answer containing tool-call markup, and returns the reason as a tool error so the model
  resubmits. The detector is `src/agent_evals/tool_markup.py`, shared with the new
  `FormatGrader` (`malformed_output` metric). Rejection messages live in `prompts.py` and are
  part of the agent version. ADR 0002 has an amendment.
- Second smoke run: `docs-20261010-182430-a355c7` (agent `59e86eb5ebdc`). 7/7 correct,
  `uncited_answer` 0/6, `malformed_output` 0/7, 25 model calls of which 23 came from the cache
  (2 paid). On 003 the first resubmission still contained `</answer>`; the second was clean with
  3 verbatim citations. `evals compare` against the baseline: `correct` +0.000 (CI 0 to 0),
  `uncited_answer` -0.167 (CI -0.50 to 0.00), both "no detectable change". Expected at n=7:
  about 3 of 7 cases must move the same way before the paired bootstrap CI excludes zero.
- Finding: `evals compare` assumes higher is better. For `uncited_answer` it printed
  "newly failing: docs-smoke-003" for a case that improved from 1 to 0, and a CI entirely below
  zero would read "regressed". The direction set `LOWER_IS_BETTER` in `report.py` is only used
  by the summary table.
- Finding: submission rejections count toward `tool_errors` and `model_calls` (003: 5 calls,
  2 errors). This is intended, they measure error recovery, but read those metrics with it in
  mind.
- Finding: the bootstrap CI collapses to [1.0, 1.0] when every case passes, which overstates
  certainty. For 7/7 a Wilson interval gives about [0.65, 1.00].

## Next tasks, in order

1. **Fix `evals compare` for lower-is-better metrics** (small). In `report.compare` and
   `cli.compare_cmd`, use `LOWER_IS_BETTER` to flip the newly passing / newly failing labels
   and the improved / regressed verdict. Add a test with a lower-is-better metric.
2. **Wilson score intervals for pass/fail metrics** in reports (keep bootstrap for continuous
   metrics and paired differences). Explain the math to Aleena first.
3. **Windows developer experience:** AGENTS.md says `make check`; on this machine use
   `uv run ruff check . ; uv run ruff format --check . ; uv run mypy ; uv run pytest`. Consider
   a cross-platform `scripts/check.py`. `.vscode/settings.json` points the interpreter at
   `.venv/bin/python`; on Windows it is `.venv\Scripts\python.exe`. Snapshot text contains
   non-breaking spaces (e.g. "Amazon S3"); consider normalizing them at snapshot time.
4. **Phase 2 (Measurement)** as described in the roadmap. Agent-drafted cases go in
   `*.drafts.jsonl` with `"provenance": "synthetic"`; Aleena verifies each against its page.

## Verified facts for writing cases (checked against AWS docs on 2026-10-10)

- New S3 buckets: Object Ownership defaults to Bucket owner enforced, ACLs disabled
  (about-object-ownership.html). Block Public Access on by default for new buckets (April 2023).
- Since January 5, 2023, all new S3 object uploads are encrypted with SSE-S3 by default
  (UsingEncryption.html).
- Single PUT: up to 5 GB. Multipart upload: single object up to 50 TB, parts from 5 MB
  (upload-objects.html). Older sources say 5 TB, so this is a good changed-fact trap.
- General purpose bucket names: 3 to 63 characters (bucketnamingrules.html).
- Lambda function timeout: 900 seconds (15 minutes); the quotas page also mentions longer
  limits for Lambda Managed Instances in some invocation modes (gettingstarted-limits.html).

## Tooling notes

- Claude Code builds; Codex reviews diffs as a second opinion. Don't have both edit the same
  branch at once.
- Pricing in `configs/default.yaml` is unset, so reports show tokens but no dollar cost and the
  per-run budget cap can't trigger until it is filled in from Anthropic's pricing page.
