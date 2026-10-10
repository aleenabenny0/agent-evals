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
