"""Model-facing text for the LLM judge. Versioned: changing a word changes the judge version.

Two verdicts (docs/plans/llm-judge.md, decided 2026-10-11). The "supported" prompt never sees the
gold answer; the "responsive" prompt does. Each is answered through one strict tool call so the
verdict is schema-checked, never parsed out of prose.
"""

from __future__ import annotations

from typing import Any

JUDGE_SUPPORTED_V1 = """\
You are checking whether an answer is supported by the evidence it cites.

You will be given a user's question about AWS documentation, an assistant's answer, and the
quotes the assistant cited from the documentation. Decide one thing only: is every factual
claim in the answer supported by the cited quotes?

- A claim is supported when a quote states it, or when it follows from the quotes by simple
  arithmetic or a unit conversion (900 seconds is 15 minutes).
- A claim that no quote states, or that a quote contradicts, makes the verdict "fail", even if
  you happen to know the claim is true. You are judging support, not truth.
- Sentences that make no factual claim (a greeting, advice to check the console) need no support.
- Do not judge whether the answer is correct, complete or helpful. That is someone else's job.

Call submit_verdict with "pass" or "fail" and a one-sentence reason. When the verdict is "fail",
the reason names the unsupported claim."""

JUDGE_RESPONSIVE_V1 = """\
You are checking whether an answer responds to the question that was asked.

You will be given a user's question about AWS documentation, a reference answer written by a
person from the documentation, and an assistant's answer. Decide one thing only: does the
assistant's answer address the question as asked and agree with the reference on substance?

- "pass" when the answer covers what was asked and says what the reference says, in any wording.
  A multi-part question needs every part answered. A question built on a false premise needs the
  premise rejected, as the reference does. Extra correct detail is not a failure.
- "fail" when the answer addresses a nearby question instead, leaves a part out, or contradicts
  the reference on substance.
- Do not judge citations, formatting or tone.

Call submit_verdict with "pass" or "fail" and a one-sentence reason."""


def verdict_tool(strict: bool) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "submit_verdict",
        "description": "Record the verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "fail"]},
                "reason": {"type": "string", "description": "One sentence."},
            },
            "required": ["verdict", "reason"],
            "additionalProperties": False,
        },
    }
    if strict:
        spec["strict"] = True
    return spec


def supported_message(question: str, answer: str, citations: list[dict[str, Any]]) -> str:
    quotes = "\n".join(
        f'{i}. ({c.get("url", "")}) "{c.get("quote", "")}"' for i, c in enumerate(citations, 1)
    )
    return f"Question:\n{question}\n\nAssistant's answer:\n{answer}\n\nCited quotes:\n{quotes}"


def responsive_message(question: str, reference: str, answer: str) -> str:
    return (
        f"Question:\n{question}\n\n"
        f"Reference answer (written by a person from the documentation):\n{reference}\n\n"
        f"Assistant's answer:\n{answer}"
    )
