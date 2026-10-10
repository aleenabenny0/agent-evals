# ADR 0002: Final answers through a schema-checked `submit_answer` tool

**Status:** accepted

## Context

Graders need structured output (answer, citations with quotes, abstention flag, confidence).
Parsing JSON out of free text is fragile. Forcing a specific tool with `tool_choice` is not
portable: some current Claude models reject forced tool choice.

## Decision

The agent ends its loop by calling a `submit_answer` tool. Tool schemas set
`additionalProperties: false` and, by default, `strict: true` so the API enforces them. The
input is validated again locally with Pydantic; an invalid submission is returned to the model
as a tool error so it can fix and resubmit. If the model stops without submitting, it gets one
nudge, then the attempt fails with `no_submission`.

## Consequences

- Graders work on typed fields, never on regex-parsed prose.
- Formatting failures (`no_submission`, invalid answers) are measured as their own failure
  type instead of being hidden inside "wrong answer".
- `strict_tools` is a config flag in case a model rejects strict schemas.

## Amendment (2026-10-10): content rules on top of the schema

A schema-valid submission can still be unusable. On the first smoke run (`docs-smoke-003`) the
model wrote its citations as tool-call markup inside the `answer` string and sent an empty
`citations` list; the schema accepted it and the citation grader flagged `uncited_answer`.

The agent now applies two content rules after Pydantic, in `submission_problems`, and returns
violations as tool errors on the same path as schema failures: an answer that is not an
abstention must carry at least one citation, and the answer text must not contain tool-call
markup (`src/agent_evals/tool_markup.py`). The rejection messages are versioned prompts in
`prompts.py` and part of the agent version. A deterministic `malformed_output` metric
(`FormatGrader`) checks the final answer for markup, so it should read zero while the guard
works and flags any agent that drops it.
