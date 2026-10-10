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

- Repo builds and all checks pass on Windows: `uv sync`, `uv run pytest` -> 38 passed.
- Docs snapshot built: 8 pages in `data/snapshots/aws-docs` (S3 x5, Lambda, DynamoDB, IAM).
  Quote matching verified on a real page.
- First real smoke run: `docs-20261010-174408-ae0812`. 7/7 correct, 0 fabricated citations,
  correct abstention on the out-of-snapshot Kinesis question, evidence recall 1.0,
  ~3.3 model calls per case, 87k input / 6.7k output tokens total. `strict: true` tool
  schemas were accepted by the API for Haiku 5.5.
- Finding: on `docs-smoke-003` the model wrote its citations inside the `answer` string
  (the answer ends with `</answer>` then `<parameter name="citations">[...]`) and submitted an
  empty `citations` list, so it was correctly graded `uncited_answer`.
- Finding: the bootstrap CI collapses to [1.0, 1.0] when every case passes, which overstates
  certainty. For 7/7 a Wilson interval gives about [0.65, 1.00].

## Next tasks, in order

1. **Fix the leaked-citations bug.** In the docs agent, reject a `submit_answer` that is not
   abstained but has no citations, or whose answer contains tool-call markup, and return it to
   the model as a tool error so it resubmits. Add a deterministic `malformed_output` metric
   that flags tool-call markup inside the answer. Tests with `ScriptedLLM`. Then (with
   permission) re-run the smoke suite and `uv run evals compare` against
   `docs-20261010-174408-ae0812`; expect "no detectable change" at n=7 and say why.
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
