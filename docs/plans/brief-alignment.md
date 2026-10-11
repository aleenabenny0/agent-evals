# Alignment with the three project briefs

**Status:** assessment, 2026-10-11. The briefs (02 Agent Evals, 01 LLM Ops, 03 Auto Routing) are
the target. This document maps what exists in this repo to Brief 02's milestones and metrics,
names every gap with the smallest action that closes it, and proposes the order for folding in
Briefs 01 and 03 as later phases. Items marked **(agree first)** touch `graders/` or `stats/`.

## Where this repo sits in Brief 02

We are option **C, research agent with citations** (fixed corpus, citation grounding): the most
hallucination-heavy option, which is why the brief recommends it when hallucination is the
headline. The agent is deliberately simple, as the brief demands; the eval system is the project.

| Brief 02 milestone | Status here | Gap | Smallest action |
|---|---|---|---|
| **M0** agent + 50 tasks, frozen holdout | Agent done. 249 drafts (6 verified). Split tool exists; holdout not yet frozen | Verification, then `evals split` | Aleena verifies; split with a recorded seed. 150/250 test exceeds the 30% minimum |
| **M1** runner, n trials, caching, trajectory logging, **flip rate** | Runner with attempts, call cache, full trajectories in DuckDB. No flip rate | Flip rate is the noise floor the brief wants | Add `flip_rate`: share of cases whose attempts disagree on `correct` (needs attempts >= 2). Note: identical runs replay from cache, so noise is measured across attempts, which use distinct cache salts **(agree first, stats)** |
| **M2** layered graders: code, trajectory, judge | Code graders done (correct, abstention, citations, format). Trajectory: evidence recall, model calls, tool errors, rejected submissions. Judge proposed | Missing: tool-selection precision/recall, arg correctness, efficiency vs reference, **loop detection**, pass@k next to pass^k | `repeated_calls` (identical tool+input pairs in one trajectory) and `pass_at_k` **(agree first)**. Tool P/R: for this agent "right tool" is "read a gold evidence page", already `evidence_recall`; argument validity is `tool_errors` |
| **M3** hallucination, operationally defined | `fabricated_citation` (page or quote invented), `uncited_answer`, `malformed_output`. No claim-level check yet | Unsupported/contradicted **claims**, **fabricated entity** (deterministic), fabricated action | Fabricated entity for this agent = a number, URL or identifier in the answer that appears in no page the agent read and not in the question: deterministic **(agree first)**. Claims: the judge's "supported" verdict (plan step 6). Fabricated action does not apply to a read-only agent; say so in the README |
| **M4** validate the judge | Proposal in `docs/plans/llm-judge.md`: kappa >= 0.7, mutation tests, bias probes, stability | Brief also wants **TPR/TNR** and a **judge-dev / judge-test** split of the labels | Add both to the proposal (done below in the revised plan) |
| **M5** error analysis, failure taxonomy | Principle exists (one cause per failure). No failed trajectories yet: smoke is 7/7 | Needs a run with failures, a trace viewer, `docs/failure-taxonomy.md` | `evals trace RUN CASE` to print a trajectory; after the first dev run, read >= 100 failures and write the taxonomy |
| **M6** continuous learning loop with gate | Designed as phase 5: failure memory, prompt optimization, champion vs challenger gate, instant rollback | Missing `CHANGELOG.md` per iteration, dataset versioning, dedup of new regression tasks | Add `CHANGELOG.md` and a `version` field on suite files when the loop starts; dedup by normalized-text similarity (no embedding dependency) |
| **M7** online evals, simulated user | Not in our roadmap | Simulated user, reference-free graders on sampled traces, dashboard, alert | New phase 7: an LLM persona generating questions, scored by the existing reference-free graders (citations, uncited, markup, fabricated entity), Streamlit dashboard, threshold alert |

### Brief 02 metrics table, item by item

| Metric | Here | Action |
|---|---|---|
| Success rate per category | `correct`, per-area possible via `area` | Per-area table in `evals report` |
| pass^k | done | |
| pass@k | missing | `pass_at_k` next to `pass_hat_k` **(agree first)** |
| Tool selection P/R | `evidence_recall` is the recall half | Precision = pages read that were gold evidence / pages read, cheap **(agree first)** |
| Arg validity | `tool_errors` | fine |
| Efficiency, loop rate | `model_calls`; no loop detection | `repeated_calls` **(agree first)** |
| Hallucination rate (response level) | `fabricated_citation` only | judge "supported" plus fabricated entity -> `hallucination` rollup |
| Claim-level unsupported rate | missing | judge, step 6 |
| Fabricated entity / action rate | missing / not applicable | deterministic number-and-URL check **(agree first)** |
| Policy violation rate | `uncited_answer`, `missed_abstention`, `wrong_abstention` are our policy rules | name them as such in the README |
| Judge agreement (TPR, TNR, kappa) | planned | add TPR/TNR and judge-test split |
| Cost per successful task, p95 latency | cost known; latency stored per attempt; neither summarized | `evals report`: cost per success, p50/p95 latency |
| Flip rate | missing | see M1 |

### Brief rules

- **Ground truth:** yes, by construction (support quotes, verified cases, deterministic graders).
- **Every claim with a number, baseline, n, CI:** Wilson and bootstrap intervals, paired comparisons, baselines agents built. The README headline numbers still have to be written once the dev run exists.
- **Show failures:** README "What broke" section, after the first dev run.
- **One command:** `scripts/check.py` runs everything offline; add `make demo` once a dev run exists.
- **Vendor-neutral, OpenAI-compatible layer:** we call the Anthropic SDK behind the `LLMClient` protocol. This is a conscious deviation: one protocol, one provider, no gateway dependency yet. Brief 03 needs several models anyway, so the OpenAI-compatible client (LiteLLM or raw OpenAI SDK) joins as a second `LLMClient` implementation then, and this becomes an ADR. The harness, graders and store never see provider types.
- **Spend control:** per-run budget cap live, every call cached. **Missing: a hard monthly limit set in the Anthropic console** (Aleena's action).
- **Secrets:** `.env` ignored, history checked; add a `gitleaks` step to CI (Brief 01 red flag).

## Revised roadmap: three briefs as one system

Order follows the briefs' own recommendation.

1. **Phase 2 (Brief 02, M0 to M4).** Verify, split, first dev and test runs, baselines, judge and its validation. Deliverable: README headline with n, CI and baselines; `docs/judge-validation.md`.
2. **Phase 2b (Brief 02, M5).** Read >= 100 failed dev trajectories, write `docs/failure-taxonomy.md` with counts and two linked traces per category.
3. **Phase 5 (Brief 02, M6), moved up ahead of the code agent.** Three gated improvement iterations with `CHANGELOG.md` entries and the improvement chart. This is "once this is built I want to make improvements in it": the chart is the single most convincing artifact in all three briefs.
4. **Phase 7 (Brief 02, M7).** Simulated user, reference-free scoring, dashboard, alert, a deliberate regression caught.
5. **Brief 01 (LLM Ops).** Wrap the docs agent as a FastAPI service: prompt registry (our versioned prompts already fit), gateway with a second provider for real failover, OTel traces, Prometheus and Grafana, eval gate in CI against `main`, canary and rollback drill, load test, runbook and postmortem.
6. **Brief 03 (Routing).** Run the suite on Haiku 5.5, Sonnet 5.5 and Opus 5.5 to build the per-model matrix; baselines (always-best, static per type, oracle); kNN and learned routers; `model="auto"` in the service from Brief 01.
7. **Phase 3 (code agent) and Phase 6 (red team)** stay, after the above, since the briefs reward depth on one agent over a second agent.

## What can be done without Aleena right now

- Report additions that are pure summaries of stored data: per-area success table, cost per
  successful task, p50/p95 latency, `evals trace` viewer, `CHANGELOG.md` scaffold, gitleaks in CI.
- Everything marked **(agree first)** waits: flip rate, pass@k, repeated calls, tool precision,
  fabricated entity. One approval covers them all; each is small and comes with tests.

## What needs Aleena

- Verification, the split seed, the judge approval and hand labels, and every paid run (unchanged).
- A hard monthly spend limit in the Anthropic console.
- A decision on the vendor-neutral deviation: keep the Anthropic SDK behind `LLMClient` until Brief 03, or add the OpenAI-compatible client now.
