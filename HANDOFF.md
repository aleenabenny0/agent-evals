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

## Current status (2026-10-11)

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
  must_include phrases and missing area; `evals split SOURCE --dev N --test M --seed S` assigns splits
  within (type, area) strata. Drafting workflow: drafts in `suites/docs/<name>.drafts.jsonl`
  with `provenance: synthetic`, `split: unassigned`, a verbatim `support` quote; Aleena verifies
  and sets `verified_by`; then `evals split`.
- Review feedback (2026-10-11): one support sentence rarely covers a whole gold answer.
  `gold.support` is now a list, one verbatim quote per fact; `evals validate` warns when a
  required phrase group appears in none of the quotes; `evals review` highlights every quote.
  75 cases gained quotes; core-028's gold was corrected to what the Block Public Access page
  actually says ("don't allow public access by default"). Also: `evals review SUITE --reviewer NAME --restore <id>`
  undoes a rejection, and a rejection now needs a reason of at least four characters.
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
- Batch 4: `suites/docs/mixed.drafts.jsonl`, 94 drafts (11 multi_step, 17 false_premise,
  12 unanswerable, 3 changed_default, 51 lookup) across databases, networking, storage, the
  remaining AI services, compute, security and devtools. All pass `evals validate`.
- **Draft totals (2026-10-11): 249 drafts in five files, 6 verified, 2 on hold.** 234 capability
  cases: lookup 146 (59% vs 35% planned), false_premise 31, multi_step 29, unanswerable 28,
  changed_default 15 (6% vs 15% planned). Areas: ai_ml 75 (30%), compute 31, networking 31,
  security 28, integration 25, storage 23, databases 16, analytics 10, management 7, devtools 3.
  The mix is lookup-heavy because quota pages yield lookups; changed_default is the scarce kind.
  Options: accept the mix and let `evals split` stratify it, or draft a small batch mined for
  changed defaults (runtime deprecations, service closures, default changes) before splitting.
- Baselines (phase 4, built early): `evals run --agent closed-book` (memory only, nothing to
  cite) and `--agent oracle-evidence` (gold pages handed over, no search). Not yet run; both cost
  money. The oracle reads gold evidence by design and never informs a release decision.
- Paraphrase robustness (phase 2 step 4): `paraphrase_of` on cases, `evals report` prints
  paraphrase consistency when the suite has groups; `suites/docs/robustness.drafts.jsonl` holds
  3 paraphrases each of the 5 verified pilot cases (dev split by design).
- Validator now also enforces `suites.held_out_services` (config): error if a held-out service
  appears in a snapshot page title or URL; warning if an unanswerable case rests on none of
  held-out service, price, SLA or date.
- Pricing for claude-haiku-5-5 is set in `configs/default.yaml` ($0.10 / $0.50 per MTok), so
  reports show dollars and the $5 per-run budget cap is live. A smoke run costs about $0.03.
- Judge proposal written: `docs/plans/llm-judge.md` (two verdicts, validation protocol, cost,
  open questions). Needs Aleena's agreement; touches graders/.
- Walkthrough of phase 2 so far: `docs/walkthroughs/2026-10-11-phase-2-authoring.md`.
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
- 2026-10-11, approved in one go (ADR 0007, walkthrough `docs/walkthroughs/2026-10-11-reliability-metrics.md`):
  flip rate and pass@k in the report (`stats/core.py`), `repeated_calls` and `evidence_precision`
  (trajectory grader v3), `unsupported_entity` from the new `GroundingGrader` (numbers and URLs in
  the answer that are in neither the question nor any page read or cited; shared extractor in
  `entities.py`). `evals validate` now also warns when a number in a gold answer has no support
  quote or is on no evidence page; `evals review` marks every answer fact (required phrase,
  number, digit-bearing identifier) in cyan around the yellow quote and lists the uncovered ones.
  The existing drafts carry 11 such warnings (3 ai-ml, 1 core, 2 integration, 5 mixed) to fix in
  one sweep once Aleena's review pass is committed (editing those files while `evals review`
  writes them would lose work). 145 tests.
- 2026-10-11: changed-default batch drafted, `suites/docs/changed-defaults.drafts.jsonl`, 31
  cases, all `changed_default`, 0 validator errors or warnings: deprecations (Lambda runtimes,
  Redshift Python UDFs, RDS magnetic, S3 SOAP), closures to new customers (SageMaker Model
  Monitor, Clarify, Ground Truth, Studio Classic, Bedrock Agents Classic, Q Business, App Runner,
  Glacier vaults), end-of-support notices (Q Developer IDE plugins, support plans, OpenSearch
  versions) and moved defaults (new-account Lambda quotas, Fargate vCPU quota and new-Region
  launch rates, PITR window, public IPv4 charges, Bedrock managed knowledge bases, 90-minute
  Lambda Managed Instances). With it the drafts are 280 and changed_default is 46 (16%), on the
  15% plan.
- 2026-10-11, later: Aleena finished reviewing the five original draft files: all 249 verified,
  none rejected (commit `9543f7a`). The missing-quote sweep then ran: 9 cases got a longer or an
  extra verbatim quote, and the number extractor learned two things AWS docs do ("1GB" and
  "10GB" are numbers with a unit stuck on; "us-west-2" is a Region name, not a 2). All six suite
  files validate with zero warnings. 156 tests. Only `changed-defaults.drafts.jsonl` (31) is
  still unverified.
- 2026-10-11, split frozen (ADR 0006 amendment): Aleena verified the changed-default batch
  (commit `b635efc`, 280 verified in all, 0 rejected). `evals split` was made paraphrase-aware
  (groups pinned to dev, like `hold`), then run once:
  `evals split suites/docs/verified.jsonl --dev 130 --test 150 --seed 20261011`. Files now:
  `suites/docs/smoke.jsonl` (7), `suites/docs/dev.jsonl` (130: lookup 74, changed_default 20,
  multi_step 12, false_premise 12, unanswerable 12, with the 2 held cases and all 20 paraphrase
  cases), `suites/docs/test.jsonl` (150: lookup 72, changed_default 26, false_premise 19,
  multi_step 17, unanswerable 16). The six `*.drafts.jsonl` files and `verified.jsonl` were
  deleted (git history has them). **`test.jsonl` is frozen: never read it for prompt, lesson,
  example or fix work.** New cases go in a fresh `*.drafts.jsonl`, get verified, and join dev.
- 2026-10-11, judge shipped (plan approved by Aleena: two verdicts, review-style labels, Sonnet
  5.5): `graders/judge.py` + `judge_prompts.py` (strict `submit_verdict` tool, forced), post-hoc
  `evals judge RUN` (never inside `evals run`), `evals label RUN --labeler aleena` (labels table,
  judge-dev/judge-test by case-id hash), `evals judge-validate RUN` (kappa, TPR, TNR, confusion),
  `evals judge-probe RUN` (number / qualifier / citation mutants must fail; padding / resample
  probes must not move). Walkthrough: `docs/walkthroughs/2026-10-11-llm-judge.md`. 176 tests.
  Judge pricing in `configs/default.yaml` is null until checked; `evals judge` reports tokens.
- **First dev run** (Aleena approved all paid runs; $100/month cap set):
  `docs-20261011-223111-702e87`, docs agent `fbfc5355d106`, dev, 3 attempts, 390 attempts,
  1568 calls, 7.66M in / 416k out tokens, **$0.97**, 21 minutes. correct **0.918** (0.874 to
  0.956); pass^2 0.885, pass^3 0.862; pass@3 0.962; **flip rate 0.100** (0.059 to 0.164);
  fabricated_citation 0.021; uncited 0; malformed 0; unsupported_entity 0.046; evidence_recall
  0.958, evidence_precision 0.910; tool_errors 0.38 per attempt (to read); model_calls 4.0;
  p50 7.5 s, p95 17.3 s; cost per success $0.0027. By area: databases 0.76 (7 cases) and
  management 0.83 (4) lowest, else 0.88 to 1.0. Paraphrase consistency 0.80 (5 groups).
  Failures worth a trace (dev, so allowed): docs-int-017 abstained 3/3, docs-int-023 answered an
  unanswerable 3/3, docs-chg-025 missed the price clause 3/3 (the question does not ask about
  price: a case defect to put to Aleena, not a gold edit), docs-ai-002 group missing "no
  additional cost" 5 times, docs-ai-001 group 7 misses (abstentions and a missing negative),
  docs-mix-050 abstained 2/3. Everything else flipped once in three.
- **All four approved runs done (2026-10-11 evening).** Baselines on dev: closed-book
  `closed-book-20261011-225242-b58341` correct 0.546 (0.460 to 0.629), $0.06; oracle
  `oracle-evidence-20261011-225523-da3dc1` correct 0.923 (0.864 to 0.958), $0.07. Paired on dev:
  docs minus closed-book -0.372 (CI -0.462 to -0.282, p=3e-12); docs vs oracle +0.005 (CI -0.046
  to +0.054): retrieval is worth 37 points and is not the bottleneck. **Test, run once:**
  `docs-20261011-225715-b67388`, 150 cases, correct **0.987 (0.953 to 0.996)**,
  fabricated_citation 0.028, unsupported_entity 0.060, $0.35; its 3 failures were not read.
  README "Results so far" carries the table.
- **Failure taxonomy written** (`docs/failure-taxonomy.md`, Brief 02 M5): all 32 failing dev
  attempts read. 13 keyword-grader false negatives, 12 hedged abstentions (right answer,
  `abstained: true`, usually after reading a sibling page instead of the top search hit), 3
  unanswerable-answered-as-false-premise (docs-int-023), 3 case defect (docs-chg-025), 0 wrong
  facts. Side finding: tool_errors are guessed URLs outside the snapshot. The grounding grader
  flagged list numerals ("1.", "2.") as unsupported numbers: fixed, grounding grader v2.
  Improvement candidates for the first gated iteration are listed in the taxonomy; none applied.
- Judge debugging: first `evals judge` run produced 346 grader errors and zero spend (Sonnet
  5.5 rejects forced `tool_choice`, 400); second attempt truncated at max_tokens 400 because the
  model thinks ~300 tokens before the tool call. Fixed both (no forcing; max_tokens 2048;
  max_tokens is now a named error). Three-answer smoke: verdicts arrive with reasons. Early
  signal: "supported" is strict and fails answers whose extra context is not in the cited
  quotes; whether that matches Aleena's reading is what kappa on judge-test will say.
- **Judge pass over the dev run** (judge `b23a6c3aef09`, Sonnet 5.5): 346 answers, 692 calls,
  723k in / 161k out tokens. `judge_responsive` 0.997 (0.991 to 1.000); `judge_supported`
  **0.177** (0.123 to 0.234). Cross-check with code: 21 of the 32 code failures are judge-
  responsive passes (the taxonomy's A, C, D exactly); the other 11 are hedged abstentions the
  judge skips. One code pass the judge failed (docs-core-028 #3, a hedged half-answer). The low
  "supported" rate is the judge failing added context that the cited quotes do not carry; by the
  brief's definition that is the claim-level unsupported rate, and whether a human agrees is what
  kappa on judge-test decides. `docs/judge-validation.md` holds the numbers and the pending bars.
- **Judge probes, three rounds** (`docs/judge-validation.md`, section 3): round one exposed two
  weak mutants (dropping "approximately" leaves a true sentence; swapping one of two citations
  leaves the other carrying the claim), round two one weak flip and one real judge miss ("up to"
  passed as "at least"), round three: number 15/15, bound flip 11/11, citation 40/40 caught;
  padding 38/40 and resample 38/40 unchanged (both exactly at the 0.95 bar; the misses are the
  same three borderline answers each round, the judge's own 5% noise). Probe rounds cost almost
  nothing after the first thanks to the call cache. Mutants and probes pass; **the judge still
  does not ship until Aleena's labels give kappa on judge-test.**
- Observations logged for the skill review: 7 (review highlight = validator set), 8 (escapes in
  edit scripts), 9 (split must treat linked cases as a unit), 10 (first failures are instrument
  failures), 11 (a mutant must be wrong).
- Found by Aleena while labeling: the review and label screens said a citation was NOT on the
  page while the grader accepted it. Cause: the screen's locator joined the quote's words with
  whitespace only, so bold markers ("**Provisioned Throughput**") blocked it; the grader compares
  normalized text. `review.quote_pattern` now rebuilds the normalized quote as a regex that lets
  markdown, link URLs, line breaks and curly typography sit between characters, and the label
  screen defers to `corpus.contains_quote` for its red/yellow verdict so it can never contradict
  the grader. Checked on all 360 support quotes in dev, test and smoke: 360 agreements, 0
  disagreements. Tests in `tests/test_quote_context.py`, including her exact sentence.
- **2026-10-12, labels in.** Aleena labeled all 114 attempt-1 answers of the dev run
  (`results/labels/docs-20261011-223111-702e87.jsonl`, via new `evals label-export`;
  `evals label --revisit` added for notes). `evals judge-validate` on judge-test (n=65):
  supported **kappa 0.256**, TPR 1.00, TNR 0.28 (29/26/0/10): the judge fails 72% of answers she
  passed, never the reverse. Responsive 114/114 agreement but no failures on either side, so
  untested. Judge-dev reading (judge-test unread): every disagreement is added context the
  quotes do not carry; her own notes fail the same kind of thing about half the time, so the
  reference is the inconsistent part. Proposal in `docs/judge-validation.md` section 2: a
  two-question rubric (core supported / fully supported), relabel judge-dev, tune, score
  judge-test once; label 30-40 closed-book answers for responsive; check the `docs-ai-002` #1
  note, which describes another case's answer.
- **2026-10-12, judge "supported" split in two** (Aleena approved both proposals): one rubric
  text (`SUPPORT_RUBRIC`) shown to the judge and on the label screen; `judge_core_supported`
  (claims that answer the question are quoted) and `judge_supported` (every claim is) from one
  call; `labels.core_supported` column; `evals label` asks both questions and `--revisit --split
  judge-dev` walks one side; `judge-validate` scores three verdicts. Validation restarts: dev run
  re-judged with the new judge, then Aleena relabels judge-dev (49), the judge is tuned if needed,
  judge-test (65) is relabeled and scored once. Closed-book answers (run
  `closed-book-20261011-225242-b58341`, ~half wrong) give "responsive" its first negatives.
- **2026-10-16, judge validation finished** (`docs/judge-validation.md`, sections 2c to 2f).
  Process in full: 114 labels, kappa 0.26 on a single "supported" question; two-question rubric;
  judge-dev relabeled, calibrated with the judge in view (`--show-judge`, judge-dev only),
  disputed pass; a label-screen bug found and fixed (page context around a quote read as cited
  text); judge-dev final core 0.79 / fully 1.00; then 65 judge-test answers labeled blind and
  fresh (`--fresh`), judged once with the final judge `2cf75fa03e09`. **Published: fully
  supported kappa 1.00 (58/0/0/7), core supported kappa 0.37 (3/2/6/54), responsive 0.68 on 36
  closed-book test labels (0.84 on all 61).** Decisions: `judge_supported` ships as the
  hallucination metric (the brief's claim-level unsupported rate); `judge_core_supported` is
  advisory, with a documented next prompt version to validate on new labels; responsive is
  reported on closed-book numbers. Labels for both runs are in `results/labels/`. Dev and
  closed-book runs re-judged with the final judge; probes re-run against it (section 3).
- **Iteration 1 run (2026-10-16 evening)**, prompt v3 challenger vs v2 champion on dev, 3
  attempts, $0.99 + judge: judge_supported 0.114 -> 0.306 (CI +0.14 to +0.25), core 0.893 ->
  0.939, correct 0.918 -> 0.900 (noise), wrong_abstention improved, everything else flat. The
  written gate tripped on the storage category (4 attempt flips of 36: 3 keyword misses the judge
  contradicts, 1 markup rejection). Record: `CHANGELOG.md`, `results/gates/iteration-1-dev.txt`.
  **Decision needed:** ship v3 (then test once, promote in `default.yaml`) or iterate. Also for
  Aleena: 9 dev cases whose keyword lists miss the page's own wording, surfaced by the shorter
  fully-quoted answers (judge says responsive): docs-ai-002-p3, docs-ai-003-p3, docs-chg-008,
  docs-chg-016, docs-chg-023, docs-core-034, docs-int-020, docs-int-041, docs-mix-079. Widening
  their `must_include` alternatives is a case fix she verifies, never a gold-to-pass edit.
  Gate-rule weakness noted for ADR 0008: a per-category band on `correct` at n = 12 trips on
  grader misses; the fix is a paired per-category interval plus a judge-responsive cross-check,
  to be adopted for iteration 2 onward, not applied retroactively to this one.
- **Iteration 1 shipped (2026-10-16, 22:40 UTC).** Gate passed under the paired-interval rule
  (`evals gate`, `results/gates/iteration-1-dev-gate.txt`). Test once: judge_supported 0.106 ->
  0.318 (CI +0.14 to +0.30), correct 0.987 -> 0.980 (noise), unsupported_entity halved.
  `prompt_version: v3` is the champion in `default.yaml`; `configs/challenger-v3.yaml` is now
  identical to it and stays as the record. CHANGELOG has the full entry. Test-split exposure: 2.
  Next iterations, in order: (2) abstention rule + read the top hit (taxonomy B; target
  wrong_abstention and pass^3, since v3 cost some run-to-run consistency); (3) judge v3
  (temperature 0, quota-row example) validated on labels from the next run's answers; then the
  simulated-user phase or Brief 01, Aleena's choice.
- **Iteration 2 rejected by the gate (2026-10-16, 23:00 UTC).** Number-coverage check at
  submit: unsupported_entity went to 0.000 but judge_supported moved only +0.02 (CI touches zero)
  and rejected submissions rose from 2% to 12%. Not shipped; v3 remains champion; CHANGELOG has the
  entry and `results/gates/iteration-2-dev-gate.txt` the gate output. Lesson: unsupported
  *numbers* are a small part of unsupported *claims*; the next lever is sentence-level.
- **Candidate judge v3** could not run: the installed Anthropic SDK's `messages.create` rejects
  `temperature` (TypeError, zero calls, zero spend): anthropic 1.13 exposes only `thinking` and
  `output_config` as sampling controls. Judge v3 is therefore rubric v3 alone; its stability is
  measured by the resample probe like any other version. Re-run on the v3 champion dev run so
  Aleena's next 40 labels (fresh answers) can score both judges.
- **Judge v3 scored the champion run** (23:06 UTC): agreement with the validated judge 95% core,
  96% fully, 100% responsive; v3 slightly stricter on core. Next for Aleena, 40 labels on fresh
  answers: `uv run evals label docs-20261016-214710-02abb7 --labeler aleena --limit 40`; then
  the agent runs `judge-validate` for both judges and picks the one with the higher core kappa
  (fully is already at 1.00 for the validated one).
- **Judge v2 vs v3 settled (2026-10-16 late).** Aleena labeled 40 fresh answers of the champion
  run. Rule committed first (`38c786f`, validation doc section 6), then scored once
  (`scripts/judge_compare.py`, `results/judge-v2-vs-v3/`): core v2 39/40 vs v3 38/40, fully 36/40
  both (kappa 0.80), responsive 40/40 both (no failures to catch). **v2 stays; v3 rejected.**
  Blind adjudication of the 5 disputed answers: the first run leaked (workflow subagents get the
  global CLAUDE.md; 11 of 25 reviewers loaded task-observer and 6 read the shared log), so it was
  re-run with `agentType: 'Explore'` reviewers (no CLAUDE.md) and audited clean, 34/34. Clean
  result: both judges wrong on docs-ai-007 (headerless rows matched to columns by elimination);
  docs-ai-023 genuinely ambiguous (blind 4-1 reads price factors as core; both readings
  defensible); docs-chg-003/004/008 labels look like slips under the rubric's plain reading.
  Aleena's decisions (2026-10-16): "the docs don't say X" is a hedge (written into draft rubric
  v4, `configs/judge-v4.yaml`, judge `c6a0e804f415`); price factors are not core for billing
  questions (wording: Aleena will add it to rubric v4). These settle docs-chg-004, docs-chg-008
  and docs-ai-023. docs-chg-003 rechecked 2026-10-17 and kept as "fully supported: yes" (stands).
  Billing wording approved by Aleena and written into rubric v4 on 2026-10-17 (judge v4 =
  `bf1a8e9f376a`).
- **Judge v2 vs v4 settled (2026-10-17): v2 stays.** Aleena labeled 40 more fresh answers
  (docs-chg-010 to docs-int-037) with rubric v4 on screen; a judge-free audit found them complete
  and consistent; exported (`7bb575c`) before judge v4 graded anything. Scored once under the
  section 8 rule (`scripts/judge_compare_v4.py`, `results/judge-v2-vs-v4/`): fully 38/40 both
  (kappa 0.86 v2, 0.87 v4), core v2 38/40 vs v4 39/40, but v4 caught 8 of 9 qualifier mutants
  (bar 90%), so rule 3 fails. The one miss is shared with v2 and is a broken mutant ("this is
  about opt-in" flipped to "exactly"); the generator now flips "about" and "approximately"
  only before a figure. Over all 349 answers v4 would read fully supported 0.326 vs 0.308.
  Also fixed: summaries, `evals compare` and `evals gate` averaged every judge's grades on a run
  (the champion run carries judge, judge-v3 and judge-v4); they now read only the configured
  judge (`-c` picks another) and the report names it. No published number changed
  (recomputed; both gates on that run were saved before a second judge graded it). Open for
  Aleena: keep v2 (the rule's verdict; recommended, since v4 measured no better on her labels) or
  one fresh probe test of v4 under a new rule committed first. Then iteration 3.
- **Iteration 3 rejected (2026-10-17).** Error analysis of v3's 82 failing dev answers
  (`docs/failure-taxonomy.md`, `results/iteration-3/`), then prompt v4, pre-registered in
  `docs/plans/iteration-3.md`. Gate failed: judge_supported 0.311 -> 0.604, but correct and
  wrong_abstention regressed. Half the new correctness failures are keyword-grader misses; with
  them fixed (counterfactual on a copy of the store) it still fails on wrong_abstention and lookup.
  (1) Done 2026-10-17: Aleena approved widening the keyword lists of 10 dev cases; all six dev
  runs regraded (CHANGELOG "Instrument" entry; README dev numbers restated; no gate decision
  changed). Note: paraphrase siblings of those cases keep the narrower lists; no current answer
  hits the gap. (2) Next: iteration 4, v4 plus an abstention guard; it will need her 30-answer
  spot check because v4-style answers are 27% shorter.
- **Iteration 4 dev gate passed (2026-10-17).** Prompt v5 (v4 plus an abstention guard,
  pre-registered in `docs/plans/iteration-4.md`, run `docs-20261017-174739-1c0e8e`):
  judge_supported 0.308 -> 0.612 with nothing regressed. Answers are 28% shorter, so before the
  test split runs, **Aleena spot-checks 30 v5 answers** (the command is in the plan's dev-result
  section; the list is fixed by a hash). The number is trusted if the judge passes at most 2 answers
  she fails on fully supported. Then: the test split once, the CHANGELOG entry, and her ship decision.
  **Spot check done (2026-10-17): failed by one.** The judge passed 3 of her fails, against a bar of 2; two
  are attributions it credits from the cited URL. Test split not run. Her labels: v5 0.60 fully
  supported (n 30) against v3 0.34 (n 80). Next step is her decision (plan, "Spot check").
  **Judge v2.1 not adopted (2026-10-17).** It halved false passes on fully supported on 30 fresh
  labels, but scored 27 against v2's 29 on core supported (validation doc section 9). v2 stays;
  the v5 number stays untrusted. Her 60 v5 labels: 0.58 fully supported. Next is her decision.
  **Retest failed too (section 10, 23 against 24); iteration 4 closed, not shipped.** v3 stays the
  champion; v5 is better by her 90 labels (0.60 against 0.34) but not proven by the judge.
  **Improvement chart done (2026-10-18)** for Brief 02 M6: `docs/charts/improvement.svg` and
  `causes.svg` in the README, rebuilt by `scripts/improvement_chart.py`. Next: phase 7 (Brief 02 M7,
  online evals with a simulated user), then Brief 01, Brief 03 and the code agent.
  **Phase 7 part 1 done (2026-10-18):** `evals simulate` and `evals pilot-review`
  (`src/agent_evals/simulate.py`, area map `configs/online-area-map.json`); code-reviewed, 16
  findings fixed; day 0, the pilot day, generated (100 questions, exact mix). The pilot command
  failed as first given (the day was an option); it was then tested end to end by an adversarial
  agent with independent reproduction, and all 10 problems found in it are fixed (plan, "Code
  review of part 1"). Next: Aleena runs the pilot from the project folder (`uv run evals pilot-review 0`) and
  makes the three plan decisions (plan, "Decisions for Aleena"); then part 2, the grader guards,
  once she approves them.
  **2026-10-19:** Aleena's decisions: grader guards and alert rule yes, static dashboard, budget ok.
  Generator v2 (lookups written from one page section, checked) passes its pre-registered checks
  (20 of 20 fully answered against v1's 6). Part 2 built (`evals online-run DAY`, commit 1e96ed6),
  under adversarial review. Prompt caching on every API call (CHANGELOG). Next: days 1 to 5 and
  their online runs, then part 3, the monitor.

## Target: the three project briefs

`docs/plans/brief-alignment.md` maps this repo to Brief 02 (Agent Evals) milestone by milestone,
lists the metric gaps with the smallest action for each, and proposes the order for Briefs 01
(LLM Ops) and 03 (Routing) as later phases. Read it before planning anything new. The headline
the briefs want: a chart of holdout success, hallucination rate and cost per success improving
over gated iterations, with a failure taxonomy shrinking beside it.

## Next tasks, in order

1. **Phase 2 (Measurement)**, plan in `docs/plans/phase-2-measurement.md`. Done without Aleena:
   drafting (280 across six files), evidence groups, paraphrase mechanics, validators, the five
   reliability/grounding metrics (ADR 0007). **Needs Aleena:**
   (a) done: all 280 drafts verified; (b) done: quote sweep; (c) done: split frozen, seed
   20261011 (see status above); (d) approve `docs/plans/llm-judge.md` and answer its three open
   questions, then hand-label 100-200 dev answers split into judge-dev and judge-test;
   (e) done: runs approved; dev docs run complete (see status), closed-book, oracle and the
   single test run were chained after it; (f) done: $100/month cap; (g) done 2026-10-17: Aleena
   decided to keep the Anthropic SDK behind `LLMClient` until Brief 03, where the
   OpenAI-compatible client joins as a second implementation with its ADR;
   (h) done 2026-10-16: judge validated (see status). **Next: the first gated improvement
   iteration** (phase 5 / Brief 02 M6), agent-side, no labeling needed: (1) a submit-time rule
   that an abstention carries no answer and an answer is not marked abstained (taxonomy B); (2)
   read the top search hit before reasoning from other pages (taxonomy B root cause); (3) cite
   what you state, including the header row with any table cell (the fully-supported rate is
   12%, and both the judge and the labeler now agree on why). Each is one change, champion vs
   challenger on dev with 3 attempts, then test once, CHANGELOG entry with intervals, ship only
   on a paired interval excluding zero with no category regressing. Aleena approves each paid
   run (about $1 dev, $0.35 test). Earlier wording of (h):
   relabel judge-dev with the two-question rubric:
   `uv run evals label docs-20261011-223111-702e87 --labeler aleena --revisit all --split judge-dev`
   (49 answers), then label 30-40 closed-book answers for responsive:
   `uv run evals label closed-book-20261011-225242-b58341 --labeler aleena --limit 40`;
   fix the `docs-ai-002` #1 note on the way; the agent then tunes the judge on judge-dev and asks
   for the judge-test relabel (65) for the one final score; (h-old) **label 100 to 200 dev answers**: `uv run evals label docs-20261011-223111-702e87
   --reviewer`-style, exact command `uv run evals label docs-20261011-223111-702e87 --labeler
   aleena --limit 50` per sitting (about two hours for 100); the agent then runs `evals judge`,
   `evals judge-validate` and `evals judge-probe` and writes `docs/judge-validation.md`;
   (i) three case decisions from the taxonomy, all dev: docs-chg-025 (gold requires an unasked
   price clause: drop the group or extend the question), docs-int-023 (reads as a false premise;
   reclassify, or keep unanswerable and let a prompt rule settle the format), docs-int-017 (a
   compound question whose second half is outside the snapshot; split it or accept the
   abstention). Case fixes, never gold-to-pass edits;
   (j) after labels exist: read `docs/judge-validation.md` (written by the agent from
   `judge-validate` and `judge-probe`) and decide whether the judge ships or its prompt revises.
   Done 2026-10-11: the changed_default batch (was (b)) and the five metric additions (was (f)).

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

- **Workflow subagents receive the global CLAUDE.md**, including its rule to start every session
  with task-observer, so they may load the skill and read or write the shared observation log.
  Any agent that must stay blind or isolated runs with `agentType: 'Explore'` (no CLAUDE.md, read
  only) and its transcripts are audited afterwards (see `docs/judge-validation.md` section 7 and
  `results/judge-v2-vs-v3/blindness-audit.txt`).

- Claude Code builds; Codex reviews diffs as a second opinion. Don't have both edit the same
  branch at once.
- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe` and logged in as
  `aleenabenny0`; a shell opened before the install may not have it on PATH, so call it by full
  path. Git pushes over HTTPS use Windows Credential Manager.
- Pricing in `configs/default.yaml` is unset, so reports show tokens but no dollar cost and the
  per-run budget cap can't trigger until it is filled in from Anthropic's pricing page.
