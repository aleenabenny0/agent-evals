# ADR 0011: A shared output contract, checked as its own metric

**Status:** accepted (2026-11-02).

## Context

`malformed_output` reads one field. It looks for leaked tool-call markup in `answer`, so `{}`,
`{"answer": null}` and `{"answer": ["63"]}` all read as well formed, and an output with no citations
field passes it. A review of the first build raised this
(`docs/walkthroughs/2026-10-10-codex-review.md`). It was left until the code agent would force a
decision on where output types live, but the answer does not depend on that agent.

The check inside the agent's loop cannot stand in for a metric. It belongs to the thing being
measured, so a change to the loop would change the behavior and its measure together. It is also
lenient: it coerces values, so the string "0.5" passes as a number.

## Decision

- **One module of contracts.** `src/agent_evals/contracts.py` holds `DocsAnswer`, `Citation` and
  `OUTPUT_CONTRACTS`, keyed by `Case.agent`. The agent and the grader both import it, and neither
  imports the other. A grader that imported the agent would pull in the model client to check a
  shape.
- **A metric of its own.** `FormatGrader` version 3 adds `output_contract`, which reads 1 when the
  whole output meets the contract. `malformed_output` keeps its meaning, so its past figures stay
  comparable.
- **Strict validation.** Nothing is coerced. The string "no" fails as a bool: in plain Python it is
  truthy, so it would read as an abstention.
- **Errored attempts are left out.** They have no final output, and `correct` already scores them 0.
  The metric asks whether finished answers are well formed.
- **An agent with no contract fails.** It reads 0 with a reason, so a new agent cannot pass by being
  unknown.
- **The gate watches it.** `output_contract` joins the monitored metrics of ADR 0008, condition 2.
- **The `submit_answer` tool spec does not change,** so the agent's version fingerprint and its gate
  receipt do not either. A test holds the spec and the contract to the same field names.

Three alternatives were dropped: importing `DocsAnswer` into the grader, for the reason above;
widening `malformed_output`, which would mix two questions and change the meaning of its old figures;
and waiting for the code agent, which would leave the gap open with nothing to gain.

## Consequences

- It catches nothing today. On the ten dev runs, all 2,584 finished attempts met the contract, and
  the other 16 of 2,600 had errored. Its value is catching a future break, not this finding.
- It checks shape, not content. It says nothing about whether an answer is right.
- All four past gates give the same decisions with the extra monitored metric
  (`results/gates/iteration-*-dev-gate-after-case-fix.txt`).
- Test and online runs were not regraded, so they hold no `output_contract` grades. New runs do. A
  gate reports the metric as skipped when one of its runs lacks it.
- The tool spec and the contract remain two statements of one shape. Generating the spec from the
  model would change the text the model reads, and so the agent's version, which would need a new
  gated run.
- The code agent adds its own model and one line to `OUTPUT_CONTRACTS`.
