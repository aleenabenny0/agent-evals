# Iteration 3: system prompt v4 (pre-registered 2026-10-17, before any v4 run)

**Change.** One prompt version, the change type ADR 0008 allows: `docs_agent.prompt_version` v3
to v4 (`configs/challenger-v4.yaml`, text in `src/agent_evals/agents/docs/prompts.py`). v4 keeps
every v3 rule except the quote-length cap and adds:

| Addition | Targets (error analysis, `docs/failure-taxonomy.md`) |
|---|---|
| A sentence-by-sentence check of the draft before submitting: quote it or delete it, then re-read each quote on its own and add the words it depends on | On a page the agent read, not quoted (82 of 157 claims, in 57 of 82 failing answers) |
| "Quote whole units" (a complete sentence, list item or table row) in place of v3's "one sentence or less" | The same cause in its commonest form, a quote cut short of the words used |
| Quote the whole table row, first cell included, plus the header row | Headerless rows (16 claims, 13 answers) |
| Say only what the quotes say: no added explanations, reasons or workarounds; keep "may", "most", "can" | Beyond the quote (27 claims, 25 answers), on no page read (4) |
| State facts directly, not "the documentation says ..." or "see the pricing page" | About the docs (27 claims, 21 answers), except statements that something is absent |
| Missing context is never a reason to abstain | Protects `wrong_abstention` |

Statements that the documentation leaves something out (about 10 of the 27 claims about the
docs) are deliberately not targeted: the labeler rules them hedges (`docs/judge-validation.md`,
section 7), so deleting them would raise the gate judge's number without better support.

**Why this and not the panel's pick.** The error-analysis workflow (`results/iteration-3/`)
proposed three changes, one per mechanism, and its panel picked a harness-side completion of
table citations because its gain is easier to detect: the agent's calls replay from cache and only
the edited answers are re-judged. It was not adopted. The adversarial check found that about 44%
of its expected dev gain comes from one table cited by 13 dev cases, 9 of them paraphrases that
never appear in test. It changes the harness's output, not the agent, and would hide a later
regression in the agent's own quoting. ADR 0008 has no change type for rewriting output after
acceptance, so adopting it would first need an ADR amendment approved by Aleena. It stays a
documented option. The per-sentence submit schema is also deferred: it is the largest code
change, and its attribution mixes a structure change with new wording.

**Prediction.** judge_supported on dev rises from 0.308 to about 0.39 (central +0.08; plausible
range +0.05 to +0.14), from the designer's compliance-discounted arithmetic. A full re-run's
paired 95% half-width at zero effect is about 0.056, from the panel's replay of the champion's
three attempts, consistent with iteration 1's interval (+0.141 to +0.245). So the gate needs a
true gain of roughly +0.06. At +0.08 it passes about four times in five; at +0.05, less than half
the time. A rejection would be recorded like iteration 2's.

**Gate.** ADR 0008 unchanged: dev, 3 attempts, paired against `docs-20261016-214710-02abb7`,
target `judge_supported` judged by the validated judge (v2, `2cf75fa03e09`). The gate fails if
the target's interval includes zero. It also fails if any monitored metric (`correct`,
`fabricated_citation`, `uncited_answer`, `wrong_abstention`, `missed_abstention`,
`rejected_submissions`) or any case type or area on `correct` regresses with its interval.
`fabricated_citation` is the metric most at risk, because whole-unit quotes are longer strings to
copy exactly.

**Checks fixed now, applied after the run.**

1. *Answer look.* If the median answer length of v4's answered attempts differs from v3's by
   more than 25%, the judge's number is not trusted until Aleena spot-checks 30 v4 answers
   against the judge, as ADR 0008 requires.
2. *No hedge deletion.* The share of answered attempts that state an absence ("does not say",
   "does not state", "says nothing", and the like) is reported for v3 and v4. A fall of more than
   half is disclosed beside the result as a possible source of gain.
3. *Diagnostic, not gating.* The same error analysis is re-run on v4's failing attempt-1 answers,
   so the changelog can show which causes shrank.

If the gate passes, the frozen test split is run once and judged once, and the CHANGELOG entry
records both splits. Shipping, which means setting `prompt_version: v4` in `default.yaml`, is
Aleena's decision. If the gate fails, v3 stays the champion and the rejection is recorded.

## Result (2026-10-17): rejected by the gate

The gate failed: judge_supported rose far more than predicted (0.311 to 0.604, +0.293, CI +0.219
to +0.368), but correct (-0.028) and wrong_abstention (+0.017) regressed, and so did lookup,
ai_ml and databases on correct. Check 1 tripped (answers 27% shorter), so the support number also
waits on a 30-answer spot check; check 2 did not (absence statements 12.0% to 7.6%). Reading the
18 new failures showed half of them to be keyword-grader misses, but a counterfactual with the
misses fixed in both runs still fails on wrong_abstention and lookup. Full entry, numbers and
files: CHANGELOG.md, "Rejected: iteration 3".

