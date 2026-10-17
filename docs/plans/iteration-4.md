# Iteration 4: system prompt v5, v4 plus an abstention guard (pre-registered 2026-10-17)

**Change.** One prompt version, the change type ADR 0008 allows: `docs_agent.prompt_version` v3
to v5 (`configs/challenger-v5.yaml`). v5 is v4 (iteration 3, rejected) with two rules added:

1. A step before the sentence check: "Before you conclude that the documentation does not
   answer something, read the first result of each of your searches that you have not read yet."
2. The abstention rule becomes: "abstained means you give no answer. If the documentation you
   can reach does not answer the question, set abstained to true and explain what is missing;
   abstaining is better than guessing. Finding that the documentation gives no date, number or
   plan is not an answer. If a quote states the core answer to what was asked, set abstained to
   false, even when part of it is uncertain: say which part you could not confirm and lower your
   confidence instead."

Every other v4 line is unchanged; a test enforces this.

**Why.** v4 raised judge_supported from 0.311 to 0.604 on dev. It failed the gate on
wrong_abstention (0.017 to 0.034), and on lookup, with correct no longer regressing once the
keyword lists were fixed. Its extra abstentions were of two kinds:

- In five, it never read the page that answers the question. That page was the first result of
  at least one of its own searches, and the agent then concluded that the docs do not say. Rule 1
  targets these.
- In four, it had the right answer in the text, or a "probably yes" from a quoted page title,
  but set abstained. Rule 2 targets these.

The two rules serve one goal, keeping v4's support gain without its abstention cost. They are
bundled because neither alone is likely to bring wrong_abstention back to v3's level.

**Adversarial review before the run** (an isolated reviewer agent, which read both runs'
stored answers). It rated the first draft "weakened". Three amendments were adopted:

- Rule 1 now names the first result of each search. The draft said "pages that could answer it"
  and allowed reading later parts of long pages. That left the choice to the judgment that had
  failed, and gave the step no bound. Projected on v4's trajectories, the draft pushed 18 of 90
  attempts past the 12-turn limit. The bounded rule pushes none and still reaches all five cases.
- Rule 2 requires a quote that states the core answer. The draft's "if your answer does answer
  the question" would have flipped two unanswerable cases that v4 abstains on with answer-shaped
  text.
- The no-date sentence was added for the same reason.

One suggestion was declined: telling the agent to write "I could not confirm" rather than "the
documentation does not say". The labeler rules the two equivalent hedges, so dictating the
phrasing would game the gate judge, which counts only the second as a claim. The cost is a
conservative bias: v5 may state absences in answered text, which the judge counts against it.

**Prediction.** judge_supported near v4's level, 0.55 to 0.62; wrong_abstention back to at most
about 0.02; correct and missed_abstention no worse than v3 within noise. The target clears the
gate on v4's evidence. The open question is whether the guard costs nothing elsewhere.

**Gate.** ADR 0008 unchanged: dev, 3 attempts, paired against `docs-20261016-214710-02abb7`,
with the keyword lists of commit `2cbd972` and the validated judge (v2, `2cf75fa03e09`).

**Checks fixed now, applied after the run.**

1. *Answer look and the spot check.* If v5's median answer length is more than 25% below v3's,
   the judge's number is not trusted, and the test split is not run, until Aleena spot-checks 30
   answers. The sample is the first 30 of v5's answered attempt-1 dev answers, sorted by
   sha256("iteration-4-spot-check:" + case id). She labels them with rubric v2 on screen and no
   judge visible. The number is trusted if the judge passes at most 2 answers she fails on fully
   supported; overall agreement is reported too.
2. *Errors.* Attempts that end in an error (for example the 12-turn limit) are invisible to the
   abstention metrics. If v5 has more than 3 errored attempts beyond v3's, that is disclosed
   beside the result.
3. *Missed abstention, case by case.* The gate cannot see fewer than about four flipped
   unanswerable cases. Every unanswerable case that v5 answers in 2 or more of 3 attempts, where
   v3 abstained, is disclosed.
4. *Attribution, diagnostic.* Each case where v5 fixes a v4 abstention or loses correctness is
   attributed from its trajectory. A newly read first result after a search points to rule 1; the
   same evidence with the flag flipped points to rule 2.
5. *Absence statements.* The share of answers stating an absence is reported for v3, v4 and v5.

If the gate passes and the spot check holds, the frozen test split is run once and judged once,
and the CHANGELOG entry records both splits. Shipping, which means setting `prompt_version: v5`
in `default.yaml`, is Aleena's decision. If the gate fails, v3 stays the champion and the
rejection is recorded.

## Dev result (2026-10-17): gate passed; spot check pending

Run `docs-20261017-174739-1c0e8e`, $1.01 agent, 704 judge calls, no errors
(`results/gates/iteration-4-dev-gate.txt`). Paired against v3: judge_supported **0.308 -> 0.612**
(+0.304, 95% CI +0.240 to +0.370); correct 0.969 -> 0.967 (no detectable change);
wrong_abstention 0.017 -> 0.008, missed_abstention 0.056 -> 0.028, fabricated_citation 0.013 ->
0.005, rejected_submissions 0.021 -> 0.008 (all no detectable change); no case type or area
regressed. **Gate passed.**

Checks (`results/iteration-4/post-run-checks.txt`):

1. Median answer length 350 -> 251 characters (ratio 0.72), so the 30-answer spot check is
   required before the number is trusted and before the test split is run.
2. Errored attempts: v3 2, v5 0.
3. No unanswerable case is answered by v5 where v3 abstained.
4. Both rules did work. Rule 1: five attempts in the docs-ai-001 group read the page that
   answers them. Rule 2: the hedged and title-only cases (docs-chg-019, docs-mix-012,
   docs-mix-050) stopped abstaining on the same evidence.
5. Absence statements: v3 12.0%, v4 7.6%, v5 8.8%.

Spot-check sample, as pre-registered: docs-int-002, docs-int-042, docs-mix-063, docs-chg-016,
docs-ai-045, docs-mix-016, docs-core-009, docs-int-030, docs-chg-030, docs-mix-003, docs-mix-049,
docs-ai-001-p2, docs-mix-023, docs-mix-071, docs-ai-004-p2, docs-core-024, docs-mix-011,
docs-core-002, docs-ai-003-p1, docs-mix-018, docs-mix-024, docs-mix-057, docs-mix-058,
docs-mix-068, docs-mix-020, docs-mix-087, docs-core-017, docs-mix-046, docs-int-004,
docs-ai-002-p1 (attempt 1 of each).

