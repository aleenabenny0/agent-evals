# Phase 2 plan: Measurement

**Status:** proposed, 2026-10-10. Becomes "agreed" once the open decisions below are settled.
Significant decisions get their own ADR as they are made.

## Goal and gate

Turn the 7-case smoke suite into a measurement instrument: enough cases, with the right mix, to
detect a 10-point change in accuracy, plus graders that can score answers keywords cannot
settle. Roadmap gate: the LLM judge passes validation (Cohen's kappa >= 0.7 against ~100 hand
labels, >= 90% of planted errors caught, no verbosity or order bias, stable on re-judging).

## Order of work

Each step is small enough to explain and review on its own. Steps 1 and 2 need no API spend.

### 1. Power analysis (stats/, needs agreement first)

**Question:** how many test cases make a 10-point accuracy change detectable?

**Why paired:** the release gate compares a candidate against the champion on the same cases.
Only cases where the two disagree carry information. Call the fraction of such cases the
**discordance** d. If the candidate is better by delta = 0.10, then among n cases
c - b = 0.10 n, where c = cases only the candidate passes and b = cases only the baseline
passes, and b + c = d n.

**Formula:** McNemar's statistic is (c - b) / sqrt(b + c) = delta * sqrt(n / d). For a
two-sided test at 5% with 80% power that must reach 1.96 + 0.84 = 2.80, so

```
n = 2.80^2 * d / delta^2  =  7.84 * d / delta^2
```

| discordance d | cases for delta = 0.10 |
|---|---|
| 0.15 | 118 |
| 0.20 | 157 |
| 0.30 | 236 |

Discordance is unknown until two real agents are compared, but 15 to 25% is typical for two
prompt versions of the same model. **150 test cases** gives 80% power at d <= 0.19, which
matches HANDOFF's 100 dev / 150 test split. The dev split has no power requirement; it needs
enough cases per failure type for lesson-writing and prompt work.

**Code:** `stats/power.py` with `cases_for_paired_delta(delta, discordance, alpha, power)` and
its inverse `detectable_delta(n, discordance, ...)`, an `evals power` command that prints the
table above, and a simulation test that generates paired outcomes at a given discordance and
checks the paired bootstrap CI excludes zero about 80% of the time at the computed n. The
simulation is the proof that the closed form and the actual comparison code agree.

### 2. Expand the snapshot (network only, no API spend)

8 pages cannot support 250 cases. Target about 40 to 50 pages across the services engineers
ask about daily, each chosen because it holds dated facts, limits, defaults, or things that
changed: S3 (object ownership, encryption defaults, limits, naming, storage classes, lifecycle,
versioning, presigned URLs, Block Public Access), Lambda (quotas, runtimes and deprecation,
timeouts, concurrency, layers, environment variables, URLs), DynamoDB (quotas, item size,
capacity modes, TTL, streams, GSI vs LSI), IAM (policy evaluation, root user, access keys,
password policy, roles vs users), plus a few from EC2, VPC, SQS, SNS, CloudWatch and STS. Pages
go in `configs/docs_pages.txt`; `evals snapshot` fetches them; the snapshot stays out of git.
Unanswerable cases need services that are **not** in the snapshot, so Kinesis, Redshift, EKS
and Route 53 stay out on purpose.

**Decision needed:** the page list. A draft list is the first deliverable of this step.

### 3. Case authoring workflow (eval integrity)

- Agent-drafted cases go in `suites/docs/<name>.drafts.jsonl` with `"provenance": "synthetic"`.
  Each draft cites the page and quotes the sentence the gold answer rests on, so verification
  is a two-minute read, not a search.
- Aleena verifies every draft against its page. Add `verified_by: str | None` to `Case`;
  `evals run` refuses a suite containing unverified synthetic cases unless `--allow-drafts`.
- `evals validate` gains a sanity check for answerable cases: at least one alternative of every
  `must_include` group should appear, after normalization, on an evidence page. A miss is a
  warning, not an error, since a gold answer may paraphrase, but it catches wrong URLs and
  invented numbers cheaply.
- Target mix for 250 cases: lookup 35%, multi_step 20%, changed_default 15%, false_premise 15%,
  unanswerable 15%. Split dev/test by stratified random assignment with a recorded seed, so
  every type is represented in both and nobody hand-picks which cases land in test.
- Hard rule, unchanged: test cases are never read when writing prompts, lessons or examples.

### 4. Paraphrase robustness suite

For a subset of dev cases, write 3 to 5 paraphrases each (`suite: "robustness"`, a
`paraphrase_of` field pointing at the source case). Metric: consistency, the fraction of
source cases whose paraphrases all agree with the source outcome. Computed by code, no judge.

### 5. Claim extraction and the NLI cross-check (open decision)

Design principle 2 puts a local NLI check between code and the judge: split the answer into
atomic claims, then check each claim against the cited quote with a natural language inference
model running locally. The decision is about dependencies: a cross-encoder NLI model needs
PyTorch (large install on a Windows CPU machine) or an ONNX runtime build (smaller). Options:

- (a) `sentence-transformers` cross-encoder, simplest code, heaviest install;
- (b) ONNX runtime with an exported small NLI model, lighter, more setup;
- (c) defer: go code -> judge for now, add NLI once the judge exists and we can measure what
  it would save.

Recommendation: (c), then revisit with judge-vs-NLI agreement data in hand. ADR when decided.

### 6. LLM judge and its validation

Judge model Claude Sonnet 5.5, pinned model id and prompt version in the run manifest. The
judge scores only what code cannot: whether the answer's claims are supported by the cited
quotes and whether the answer actually answers the question. Validation protocol before any
judge number counts:

1. ~100 hand labels from Aleena on dev-split answers (this doubles as the first human review
   queue), Cohen's kappa >= 0.7.
2. Mutation tests: take correct answers, plant one error each (wrong number, wrong service,
   dropped qualifier), judge must catch >= 90%.
3. Bias probes: same content, longer vs shorter; same pair, swapped order.
4. Stability: re-judge the same set; agreement with itself.

All judge calls go through `LLMClient`, so they are cached and counted like agent calls.

## Open decisions, in the order they block work

1. Agree the power-analysis approach above (step 1).
2. Approve the snapshot page list (step 2).
3. `verified_by` on `Case` and the `--allow-drafts` guard (step 3).
4. NLI option (a), (b) or (c) (step 5), can wait until step 6 is underway.

## What does not change

Everything from phase 1 stays as the floor: deterministic graders run on every attempt, every
run has a manifest, the call cache makes re-runs free, and no change ships without a paired
comparison whose interval excludes zero.
