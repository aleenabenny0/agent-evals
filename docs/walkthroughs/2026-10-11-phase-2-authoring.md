# Walkthrough: Phase 2 so far, the measurement instrument

**Summary.** This walkthrough covers phase 2 so far: what was built after the leaked-citations
fix to turn the suite into a measurement instrument, in the order it landed. A power calculation
set the size at 150 test cases plus about 100 dev cases, and the documentation snapshot grew from
8 pages to 334 across roughly 100 services, with a monthly drift check and evidence recorded as
groups of alternative pages. Cases are drafted by an agent, but each carries a support quote,
copied word for word, that `evals validate` checks against the page in the snapshot, and
`evals run` refuses to score a draft (a synthetic case with no `verified_by`) unless
`--allow-drafts` is passed. The pilot review of the first 49 AI/ML drafts found two AWS pages
that disagree, keyword rules that passed wrong answers, a grader bug that matched substrings, and
a mix that was too easy. Closed-book and oracle baselines were built early, and `evals report`
computes paraphrase consistency; at the time of writing, verification, the split and the judge
were still ahead.

Covers everything built after the leaked-citations fix, in the order it landed, with the
reasoning in plain language. Companion to `docs/plans/phase-2-measurement.md` and ADRs 0004 to
0006. Written so each piece can be explained from this page alone.

## 1. Why the suite needed to grow, and by how much

Seven smoke cases catch gross breakage but cannot measure a change. The release gate compares a
candidate against the champion on the same cases, so only cases where the two **disagree** carry
information; call that share the **discordance** d. For a 10-point difference delta, McNemar's
statistic is `delta * sqrt(n / d)`, and detecting it with 80% power at the 5% level needs
`n = 7.84 * d / delta^2`. At d = 20% that is 157 cases. `evals power --cases 150` prints the
table; `tests/test_power.py` simulates paired outcomes and checks the formula against the paired
bootstrap that `evals compare` really uses, so the number is not a textbook quote. Decision: 150
test cases plus about 100 dev cases.

## 2. The snapshot became the platform, not four services

From 8 pages to 334 across roughly 100 services, with AI and ML as the named focus (Bedrock,
AgentCore, Nova, SageMaker AI, the language, vision and speech services, OpenSearch vector
search). Two to four pages per service: an overview, the quotas page, one or two fact-dense
feature pages. Every URL was checked to return 200 without a redirect before it went into
`configs/docs_pages.txt`; four pages that had moved were replaced, and two redirects that
carried a suspicious query string were replaced by clean canonical URLs.

More pages than cases is intentional. Retrieval has to find the right page among hundreds, which
is the realistic setting. The first smoke run on the big snapshot showed the effect: 7/7 still
correct, but model calls per case rose from 3.9 to 5.3.

## 3. Keeping the snapshot honest: the monthly drift check (ADR 0005)

A snapshot goes stale silently. `evals snapshot` now also writes `configs/docs_pages.lock.json`,
one hash per page and no content, so it can be committed. `evals snapshot --check` refetches
every page, compares hashes, and lists the suite cases whose evidence pages changed.
`.github/workflows/docs-drift.yml` runs it on the first of each month and opens a `docs-drift`
issue with the report. Nothing is edited automatically: a human re-verifies the listed cases,
which keeps the rule that gold answers never change to make the agent pass. Validated twice the
day it shipped, locally and on GitHub's runner: 333 pages unchanged, no drift, no issue.

## 4. Evidence groups, because facts live on several pages

With 334 pages, the Lambda timeout appears on three of them. `gold.evidence` used to be a flat
list of required URLs, so an agent that grounded a correct answer on the "wrong" page lost
recall. It is now a list of **groups of alternatives**: every group must be read or cited, any
URL in a group satisfies it. A flat list still parses, as one group per URL. The grader counts
groups hit, so recall stays a fair measure of "did it find supporting evidence".

## 5. Agent-drafted cases without agent-drafted mistakes (ADR 0006)

Writing 250 cases by hand is slow; letting an agent draft them risks exactly the failure the
project measures. The design makes the risk mechanical:

- Every draft carries a **support** quote: the sentence on an evidence page the answer rests on,
  copied word for word. `evals validate`, when a snapshot is present, proves the quote exists on
  the page. An invented fact needs an invented quote, and the invented quote fails.
- A synthetic case with no `verified_by` is a **draft**. `evals run` refuses to score drafts or
  unassigned cases unless `--allow-drafts` is passed. Nothing unverified can leak into a number.
- `evals review` shows each draft with the quote highlighted inside the surrounding page text and
  records accept or reject; rejected cases move to a `.rejected.jsonl` file with the reason.
- `hold` keeps a case out of the test split for a stated reason. Two pilot cases are held
  because keyword grading cannot tell the right recommendation from the wrong one; the judge
  will settle them.
- `evals split SOURCE --dev N --test M --seed S` assigns verified cases within (type, area)
  strata with a recorded seed, so nobody hand-picks the test set, and held cases always land in
  dev.
- Unanswerable cases encode what the corpus **lacks**. Growing the snapshot broke one silently:
  the Kinesis quotas page arrived and a "no Kinesis docs" case became a minimum-versus-default
  trap. The fix was structural: `suites.held_out_services` in the config, enforced by `validate`
  (error if a held-out service appears in a page title or URL; warning if an unanswerable case
  rests on none of held-out service, price, SLA or date). The case was replaced under a new id,
  because a changed question must never be compared across runs under the old id.

## 6. What the pilot review taught

The author's review of the first 49 AI/ML drafts, after Codex ran a second-opinion pass:

- **Two AWS pages disagree.** The Provisioned Throughput page says a customized Bedrock model
  requires Provisioned Throughput; the newer custom-model inference page documents on-demand
  deployments. One case became a changed-default case citing the newer page; one was removed
  because no page supported its conclusion. Documentation is not a single source of truth, and an
  agent that reads only the older page gives the outdated answer.
- **Keyword rules that passed wrong answers.** "until the" accepted anything; "lower" accepted
  "can never be lower". Rules were tightened to require the affirmative wording. Keyword grading
  cannot see negation, which is the standing argument for the LLM judge.
- **A grader bug.** `must_include` matched substrings, so `5 MB` passed inside `15 MB` and `10`
  inside `100`. `phrase_in` in `corpus.py` now requires whole words and numbers, with the edges
  tuned so `/16`, `10%` and `3-63` still match. The grader and the validator share it.
- **Reviews describe; they do not edit.** Codex's fixes were written in past tense and did not
  exist on disk. Rule since: a described change is not done until the diff shows it.
- **The mix was too easy.** 40 of 49 pilot cases were lookups. Later batches were built hardest
  first. Across the 234 capability drafts the realized mix is still lookup-heavy (ADR 0006
  gives the mix of the final split), because quota pages yield lookups; changed-default cases
  are the scarce kind and are worth mining deliberately (deprecation schedules, closures like
  Cloud9 and Kendra, default changes like S3 bucket quotas, DynamoDB on-demand, Organizations
  root credentials).

## 7. Baselines (phase 4, built early because they need no new infrastructure)

`evals run --agent closed-book` answers from model memory with no documentation tools, so its
`correct` rate is the bar the retrieval agent must beat and its `uncited_answer` rate is 100% by
construction. `--agent oracle-evidence` is handed the gold evidence pages, so the gap between it
and the real agent is the cost of retrieval mistakes. Both reuse the same loop, graders and
version fingerprint; the oracle reads gold evidence on purpose and must never inform a release
decision.

## 8. Paraphrase robustness (phase 2 step 4)

A robustness case names the case it paraphrases and shares its gold. `evals report` computes
**paraphrase consistency**: the share of groups whose source and paraphrases all got the same
`correct` outcome. A model that answers a question right but its rewording wrong is not robust,
and this measures that independently of whether the answers were right. Fifteen paraphrases of
the five verified pilot cases are drafted; robustness cases stay in dev.

## 9. Questions a reader might ask

**Why not let the judge grade everything?** Because code never drifts and costs nothing. The
deterministic graders settle what they can settle; the judge is reserved for what they cannot,
and it has to pass validation against hand labels before any of its numbers count.

**How do you know the drafts are right?** Drafting does not establish that; verification does.
The design's job was to make verification a two-minute read with a machine-checked pointer, and
to make it impossible for an unverified case to be scored.

**What is left?** Next: mine changed-default cases deliberately, finish verification, split,
then build and validate the judge. Then the learning loop in phase 5 can start, because only
then is there a trustworthy signal to learn from.
