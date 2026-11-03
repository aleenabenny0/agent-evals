# Phase 9: routing between Haiku, Sonnet and Opus (Brief 03, Auto Routing)

**Summary.** The docs agent runs on Haiku 5.5. This phase runs the same agent, with the same prompt,
tools and limits, on Sonnet 5.5 and Opus 5.5, builds a matrix of what each model buys and costs on
each question type, and tests whether a router that picks a model per question beats a single model.
Haiku's part of the matrix comes from runs already stored, so it costs nothing. Estimated cost $24 to
$47, capped at $45.

**Status:** approved 2026-11-03, with decisions 1b, 2a and 3a, and the ledger capped at $45.

**Goal.** Know, with intervals, what a larger model buys on this agent, and whether choosing a model
per question gives the same quality for less.

## The headroom problem, stated first

On the frozen test split Haiku answers 147 of 150 questions correctly (0.980). A router can win at
most those 3 questions on `correct`, and an exact paired test on 3 questions that all go one way
gives p = 0.25 at best. So on `correct` alone this phase could only show that a larger model buys
nothing measurable.

Support has room. The validated judge finds 31.8% of Haiku's test answers fully supported by the
quotes they cite, and 30.9% on dev. So the routing target is a **good answer**: correct, and either
an abstention or fully supported by the judge (decision 2). An abstention has no claims to support,
so a correct one counts as good.

## Part 1: the matrix

- **Runs.** Sonnet 5.5 and Opus 5.5, each on dev (130 cases, 1 attempt) and the frozen test split
  (150 cases, 1 attempt), with prompt v3. Only the model, its prices and the run budget change, in
  `configs/model-sonnet.yaml` and `configs/model-opus.yaml`. Haiku's runs are
  `docs-20261016-214710-02abb7` (dev, 3 attempts) and `docs-20261016-222256-04f0f4` (test), on agent
  version `8784b07dbd32`, which behaves the same as the shipped `3b34d1c3829d` (its gate receipt).
- **One attempt each.** Every model is compared on attempt 1 of each question, so the matrix is
  paired and no model gets more tries than another. Haiku's three-attempt figures are shown beside.
- **Costs are compared at list price with no prompt cache,** from each answer's tokens. Haiku's
  runs predate the prompt cache, so billed costs would favor the new models. Billed costs are
  reported beside. The analysis is `scripts/phase9_routing.py`.
- **The judge.** The validated judge (v2, as in `configs/default.yaml`) grades every new answer, as
  it already graded Haiku's.
- **Reported per model and per question type:** correct, good answer, judge fully supported,
  abstention, errors, cost per answer, cost per good answer, and median and 95th percentile
  latency, each with a Wilson or bootstrap interval.
- **A pilot first.** The first 10 dev cases, 1 attempt per new model, to measure the real cost per
  answer and the prompt cache's share, and to check that answers fit in 2,048 output tokens. If the
  cost measured projects past the cap, the phase stops there and the author decides. The pilot
  costs nothing extra: the call cache keys each reply by case and attempt, so the dev runs replay
  its 20 answers.

## Part 2: the routers (agree first)

Every router is fitted and tuned on dev only. Test is read once, after the chosen router's settings
are committed.

| Router | How it picks | Usable on a live question |
|---|---|---|
| Single model | Always Haiku, always Sonnet, or always Opus | yes |
| By type | Per question type, the cheapest model within 2 points of the best dev good-answer rate for that type | no: the type is a label on a case, not known for a live question. Shown for comparison |
| Cascade | Haiku answers first. If it abstains, errors, breaks the output contract or cites nothing, Sonnet answers instead, and Haiku's cost is still paid | yes |
| Nearest neighbours | TF-IDF over the question text. A question's k most similar dev questions predict each model's good-answer rate, and the cheapest model within τ of the best prediction answers. k and τ are chosen by leave-one-out on dev | yes |
| Oracle | Per question, the cheapest model that gave a good answer | no: it reads the outcome. An upper bound |

**The one claim, fixed before test is read.**

- **Always-best** is the single model with the highest dev good-answer rate.
- **The chosen router** is the usable router with the highest leave-one-out dev good-answer rate
  among those cheaper than always-best. Ties go to the cheaper router.
- **On test:** the chosen router's good-answer rate minus always-best's, paired over the 150
  questions, with a 95% bootstrap interval, and its cost per answer as a share of always-best's.
- **"As good for less"** is claimed only if the interval's lower end is above -5 points and the
  router costs less. **"Better than Haiku alone"** is claimed only if its paired interval against
  always-Haiku excludes zero.
- **Power.** Before the test look, `stats/power.py` gives the smallest difference 150 paired
  questions can detect at the discordance seen on dev, and that figure is reported with the result.

Every router's test result goes on one cost-against-quality chart, but only the chosen router makes
a claim.

**Two choices, both frozen before test.** The rules above pick one router on good answers and one
on `correct`, and both are committed before the test runs. The spot check below then decides which
claim is made, not which router is chosen, so it can come after the test runs without letting them
shape the choice.

**The judge on new models.** The judge was validated on Haiku's answers. A later spot check found it
too lenient on another prompt's shorter answers. So for each new model, 30 answered attempt-1 dev
answers, the first 30 sorted by sha256("phase-9-spot-check:" + model + ":" + case id), are labeled
blind with rubric v2, with no judge verdict on screen. That model's judge numbers are trusted if
the judge passes at most 2 answers the labeler fails, the rule of iteration 4. If a model fails,
its judge numbers are reported as untrusted and the claim is made on `correct` only.

## Dev result and the frozen choices (2026-11-03)

The pilot measured $0.016 per answer on Sonnet and $0.033 on Opus, below the low estimates: both
took 3 model calls per answer where Haiku takes about 4. The Opus dev run
(`docs-20261103-042856-b3bc58`) lost 4 attempts to connection errors that outlasted the SDK's 5
retries. A rerun (`docs-20261103-044411-eb598a`) replayed every other call from the call cache and
retried those 4 for $0.15, so its answers are the first run's plus the 4 recovered. One error is the
model's own and stays: on `docs-core-001` Opus never made a valid submission. Both new runs were
judged by the validated judge (`2cf75fa03e09`).

Dev, attempt 1, 130 questions (`results/routing/dev-fit.json`):

| | Good answer | Correct | List cost per answer | Answered, fully supported |
|---|---|---|---|---|
| Haiku | 0.331 (0.256 to 0.415) | 0.954 | $0.0025 | 0.287 of 115 |
| Sonnet | 0.500 (0.415 to 0.585) | 0.962 | $0.0352 | 0.475 of 118 |
| Opus | 0.615 (0.530 to 0.695) | 0.969 | $0.0755 | 0.593 of 118 |
| Cascade, Haiku then Sonnet | 0.346 | 0.985 | $0.0098 | |
| Nearest neighbours, leave-one-out | 0.592 (k 15, tau 0) | 0.954 (k 15, tau 0.2) | $0.0679 and $0.0025 | |
| Oracle (reads outcomes) | 0.761 | 0.992 | $0.0251 and $0.0057 | |

The choices, frozen with sha256 `d1698d6b8388e2d5443a906597e43603701a0488697559d3f7cc514c9bb1fdf8`
before any test run:

- **Good answers:** always-best is Opus. The chosen router is nearest neighbours with k 15 and
  tau 0, at 90% of Opus's cost on dev. Its dev answers differ from Opus's on 5.4% of questions, so
  150 test questions can detect a difference of about 5.3 points.
- **Correct:** always-best is Opus. The chosen router is the cascade, at 13% of Opus's cost on dev.
  Its dev answers differ from Opus's on 3.1% of questions, so about 4.0 points is detectable.

Spent so far: $8.59 on the agent, and about $1.10 on the judge, estimated at its measured $0.0048
per answer.

## Part 3: `model: auto` in the service

If the chosen router is usable, the service gains `model: auto`, which picks the model per request
and keeps failover per model call. The response and the metrics name the model that answered. This
part is free and tested offline.

## Part 4: the record

ADR 0012 for the routing design, the matrix and the chart in the README, and a CHANGELOG entry. A
result that routing does not pay on this agent is a result, and is reported the same way.

## Not in this phase: the failover drill

Phase 8's two remaining steps need a second provider other than Anthropic. Phase 8 ruled out a
local model, since this machine has no GPU, and the project has no budget for a second paid
provider, so those steps stay not run. Failing over to another Claude model would run the code, but
it would not protect against a provider outage, so it does not count as the drill.

## Cost and human time

Each answer is priced from the token counts of Haiku's v3 answers (19,885 input and 1,086 output
tokens on dev, 18,333 and 1,039 on test) at $2 and $10 per million input and output tokens for
Sonnet 5.5, and $4 and $20 for Opus 5.5, read from the pricing page on 2026-11-03. The low end assumes
the prompt cache serves as much as on the last cached dev runs, about 70% of input at 0.05 times the
price, and the high end assumes it serves none. A larger model may take more steps or fewer, which
the pilot measures.

| Step | Answers | Estimate |
|---|---|---|
| Pilot | 20 | part of the dev runs below |
| Sonnet, dev | 130 | $3.50 to $6.60 |
| Opus, dev | 130 | $6.90 to $13.20 |
| Sonnet, test | 150 | $3.70 to $7.10 |
| Opus, test | 150 | $7.50 to $14.10 |
| Judge, all new answers | 560 | $2.80 to $5.60 |
| **Total** | | **$24 to $47**, capped at $45 |

The agent asks for the prompt cache on every call, so the low end is expected. Each run also stops
itself past its `budget_usd`, $8 for Sonnet and $15 for Opus, above one run's high estimate. With 3
attempts on dev (decision 1a, not taken), the total would have been $49 to $93.

**Order.** Pilot, the two dev runs, the judge on dev, both router choices committed, the two test
runs, the judge on test, then the test look. The spot check can happen at any point after the dev
judge and before the claim is written.

Human time: about 10 minutes for the decisions, and about an hour to label the 60 spot-check
answers.

## Risks

- **The prompt was tuned on Haiku** over four gated iterations, which may favor it. That is stated
  beside the result.
- **Larger models may write longer answers,** past 2,048 output tokens. The pilot checks, and
  truncated answers count as errors, not as quiet passes.
- **The judge is a Sonnet model judging Sonnet's answers,** among others. The spot check is the
  guard.
- **Rate limits on Opus.** Runs use 4 workers, which can be lowered with no effect on results.

## Decisions

Approved 2026-11-03: 1b, with the cap lowered to $45; 2a; 3a. Approval covers the paid steps up to
the cap.

1. **Scope and budget.** (a) 3 attempts on dev, a ledger capped at $100. *Recommended.* It matches
   Haiku's runs, so the matrix is paired, and gives the routers three outcomes per question to learn
   from. (b) 1 attempt on dev, capped at $50. The routers learn from one noisy outcome per question.
2. **What a router aims at.** (a) Good answers, with the spot check above. *Recommended.* It is
   where the room is. (b) `correct` only. No labels needed, but almost no room: at most 3 test
   questions to win.
3. **The failover drill.** (a) It stays not run, as above. *Recommended.* (b) Fail over to another
   Claude model, reported as a model failover and not a provider failover.
