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

## Current status (2026-10-10, end of day)

- Repo builds and all checks pass on Windows: `uv sync`, `uv run pytest` -> 89 passed. Published
  at https://github.com/aleenabenny0/agent-evals (public). CI (`.github/workflows/ci.yml`, Ubuntu)
  was green on the first push.
- Docs snapshot built: 8 pages in `data/snapshots/aws-docs` (S3 x5, Lambda, DynamoDB, IAM).
- Three smoke runs, all 7/7 correct, 0 fabricated citations, correct abstention on Kinesis:
  - `docs-20261010-174408-ae0812` (agent `3960330eeb86`, prompt v1). Case 003 leaked its
    citations as tool-call markup inside `answer` and was graded `uncited_answer`.
  - `docs-20261010-182430-a355c7` (agent `59e86eb5ebdc`), after the submission guard. 003 was
    rejected twice, then submitted cleanly with 3 verbatim citations. 23 of 25 calls from cache.
  - `docs-20261010-205341-14beef` (agent `ef8b253524b8`, prompt v2; graders citations 2,
    output_format 2, trajectory 2). 0 rejected submissions, 0 tool errors, 27 calls, none cached
    because the prompt changed, 116k in / 7.5k out tokens. `evals compare` against both earlier
    runs: "no detectable change" on every metric, as expected at n=7 (about 3 of 7 cases must
    move the same way before the paired bootstrap CI excludes zero).
- Task 1 (leaked citations) is done and was reviewed by Codex. The review and the responses to
  it are in `docs/walkthroughs/2026-10-10-codex-review.md` and section 7 of
  `docs/walkthroughs/2026-10-10-leaked-citations-fix.md`. What exists now:
  - Docs agent: `_check_submission` (Pydantic shape check, then `submission_problems` content
    rules: cite or abstain, no tool-call markup). Rejections go back as tool errors; after
    `docs_agent.max_submission_rejections` (3) the attempt fails as `malformed_submission` with
    the last reason. System prompt v2 tells the model it may resubmit. The agent version hashes
    the whole config, every model-facing text in `prompts.py`, the detector pattern and tools.
  - `src/agent_evals/tool_markup.py`: case-sensitive detector that requires `name=` on opening
    tags, so AWS placeholders and XML never trigger it; labeled fixtures in
    `tests/test_tool_markup.py`.
  - Graders: `FormatGrader` (`malformed_output`), `rejected_submissions` in the trajectory
    grader, graders never raise on malformed citations, `graders.min_quote_chars` in config,
    grader versions recorded in the run manifest. Corpus moved to `agent_evals/corpus.py` so
    graders no longer import from the agent package.
  - `evals compare` honors metric direction (verdict and newly passing/failing labels) and says
    so when a metric is missing from one of the runs.
- One definition of the checks: `uv run python scripts/check.py` (Makefile `check` and CI both
  call it; `--fix` auto-formats first). `.vscode/settings.json` no longer hard-codes a Linux
  interpreter path; VS Code discovers `.venv` on any OS. Non-breaking spaces in snapshot text
  were already handled by `normalize()` (NFKC) and the search tokenizer; a test now pins that.
- Fourth smoke run, on the 333-page snapshot: `docs-20261010-224316-1efb7b` (agent
  `ef8b253524b8`, corpus `9a1e3a9463da`). 7/7 correct, 0 fabricated, 0 rejected submissions,
  evidence_recall 5/6 (005 answered the Lambda timeout correctly from a different Lambda page
  than the one listed as evidence), model_calls 5.3 per case (was 3.9 on 8 pages), 37 calls,
  287k input tokens. `evals compare` against run 3 says "no detectable change" and flags that
  the snapshots differ.
- `gold.evidence` is now a list of groups of alternative URLs (a flat list is shorthand for one
  group per URL); `evidence_recall` counts groups with at least one URL read or cited. Smoke
  case 005 lists the three Lambda pages that state the timeout (ADR 0006 amendment).
- Phase 2 step 3 done (ADR 0006): `area`, `verified_by`, `gold.support` on cases; `split`
  `unassigned`; `evals run` refuses drafts/unassigned without `--allow-drafts`; `evals validate`
  with a snapshot errors on missing evidence pages and unfound support quotes, warns on absent
  must_include phrases and missing area; `evals split --dev N --test M --seed S` assigns splits
  within (type, area) strata. Drafting workflow: drafts in `suites/docs/<name>.drafts.jsonl`
  with `provenance: synthetic`, `split: unassigned`, a verbatim `support` quote; Aleena verifies
  and sets `verified_by`; then `evals split`.
- Pilot draft batch: `suites/docs/ai-ml.drafts.jsonl`, 49 cases (40 lookup, 2 false_premise,
  2 multi_step, 1 changed_default, 4 unanswerable) drafted from 13 AI/ML pages (Bedrock
  cross-Region inference, inference profiles, quotas, model access, batch inference, Provisioned
  Throughput, Guardrails, Knowledge Bases; SageMaker serverless and async inference; Comprehend,
  Rekognition and Polly quotas). All pass `evals validate` against the snapshot. **Aleena's
  step:** for each case open the evidence page, read the question, gold answer and `support`
  quote, then add `"verified_by": "aleena"`; change a gold answer only if the page says
  otherwise. Fastest way: `uv run evals review suites/docs/ai-ml.drafts.jsonl --reviewer aleena`
  shows each draft with the quote highlighted in its page and records accept/reject; rejected
  cases move to `ai-ml.rejected.jsonl` with the reason. Next batches should lean to multi_step and changed_default (Lambda runtime
  deprecations, S3 and IAM defaults, Kubernetes versions, CodeCommit/Cloud9 closure).
- Pilot review (Aleena, 2026-10-10): two cases assumed a customized Bedrock model needs
  Provisioned Throughput. `prov-throughput.html` still says so, but the newer
  `model-customization-use.html` (now in the snapshot) documents on-demand custom model
  deployments. 022 is rewritten as a changed_default case citing the newer page; 025 is removed.
  **Two AWS pages disagreeing is a strong interview example.** Keyword rules that passed wrong
  answers were tightened (011, 012, 024, 031, 036), rules that failed correct phrasings were
  widened (009), 015's 2-minute wording was fixed, 030 asks for an example instead of a complete
  list. 007 and 029 carry `hold`: keyword grading cannot tell the right recommendation from the
  wrong one, so `evals split` keeps them in dev until the judge exists. Pilot now 48 cases, 5
  verified. Grader bug found and fixed: must_include matched substrings ("5 MB" inside "15 MB");
  `phrase_in` in corpus.py now requires whole words and numbers and is shared by
  `AnswerGrader` (version 2) and `evals validate`.
- Lesson: a review done by Codex is a description, not a change. Codex tests against an
  in-memory copy and writes nothing; apply its fixes in this session and re-run `evals
  validate` before trusting them.
- Batch 3: `suites/docs/integration.drafts.jsonl`, 47 drafts built hardest-first after the pilot
  review (12 multi_step, 8 false_premise, 7 unanswerable, 3 changed_default, 17 lookup) across
  Step Functions, SQS, API Gateway, EventBridge, Kinesis, Firehose, Secrets Manager, ACM,
  CloudFormation and Parameter Store. All pass `evals validate`. Totals: 140 drafts, 5 verified.
- Smoke suite: `docs-smoke-007` (Kinesis, unanswerable) was retired because the broadened
  snapshot includes the Kinesis quotas page; `docs-smoke-008` (Amazon Chime SDK, held out)
  replaces it. Runs before and after share 6 smoke cases, not 7.
- Batch 2: `suites/docs/core.drafts.jsonl`, 45 drafts leaning to the under-represented types
  (7 changed_default, 5 multi_step, 4 false_premise, 24 lookup, 5 unanswerable) across compute
  (Lambda runtimes, EKS versions), storage (S3 bucket quotas), networking (VPC CIDR and quotas),
  databases (DynamoDB capacity mode), security (IAM root and access keys, KMS rotation) and
  devtools (Cloud9 closure). All pass `evals validate`. Pilot status: 5 of 49 AI/ML cases
  verified, 0 rejected. Totals so far: 94 drafts, 5 verified, target ~250.
- Docs drift check (ADR 0005): `evals snapshot` writes `configs/docs_pages.lock.json` (page
  hashes, committed); `evals snapshot --check [--report f.md]` fetches and compares, exit 1 on
  drift, listing cases whose evidence pages changed; `.github/workflows/docs-drift.yml` runs it
  monthly and opens a `docs-drift` issue. When that issue appears: run `uv run evals snapshot`,
  re-verify the listed cases against their pages, commit the lock. `evals compare` warns when
  two runs used different snapshots.
- Phase 2 started (plan: `docs/plans/phase-2-measurement.md`). Step 1 done: `stats/power.py`
  and `evals power` (McNemar normal approximation, simulation-tested against the paired
  bootstrap); 150 test cases detect a 10-point change at discordance up to ~19%. Step 2 drafted:
  `configs/docs_pages.txt` lists 333 pages across roughly 100 services (AI/ML as the named
  focus: Bedrock, AgentCore, Nova, SageMaker AI, Comprehend, Rekognition, Textract, Transcribe,
  Polly, Translate, Kendra, Lex, Amazon Q, Personalize, OpenSearch; plus compute, databases,
  analytics, integration, networking, security, management, dev tools, storage, migration),
  every URL checked to return 200 without redirect. Held out for unanswerable cases: GameLift,
  Ground Station, Braket, RoboMaker, Snow Family, Chime SDK, IoT Greengrass, WorkSpaces,
  MediaConvert, Managed Blockchain, Outposts. Awaiting Aleena's approval before
  `uv run evals snapshot` fetches them.
- Pass/fail metrics now get Wilson score intervals (ADR 0004,
  `docs/walkthroughs/2026-10-10-wilson-intervals.md`); the report table shows the method per
  row. 7/7 on `correct` reads 0.646 to 1.000 instead of 1.000 to 1.000. Counts, fractions,
  multi-attempt runs and paired comparisons keep the bootstrap.

## Next tasks, in order

1. **Phase 2 (Measurement)**, plan in `docs/plans/phase-2-measurement.md`. Remaining, in order:
   (a) Aleena verifies the pilot batch (`evals review`); (b) done: evidence groups; (c) draft
   the remaining ~200 cases across areas per the mix,
   leaning to multi_step and changed_default; (d) `evals split <verified> --dev 100 --test 150
   --seed <record it>`; (e) paraphrase robustness suite; (f) LLM judge and its validation.

2. **Output-contract metric** (Codex P3, deferred). `malformed_output` only checks the
   `answer` field for markup; an output like `{}` or `{"answer": null}` scores 0. Add a check of
   the whole output shape against a shared typed contract as its own metric. Decide where shared
   output types live when the code agent (phase 3) arrives, so the decision is made once; ADR.

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
- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe` and logged in as
  `aleenabenny0`; a shell opened before the install may not have it on PATH, so call it by full
  path. Git pushes over HTTPS use Windows Credential Manager.
- Pricing in `configs/default.yaml` is unset, so reports show tokens but no dollar cost and the
  per-run budget cap can't trigger until it is filled in from Anthropic's pricing page.
