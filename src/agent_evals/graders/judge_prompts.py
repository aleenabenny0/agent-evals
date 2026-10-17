"""Model-facing text for the LLM judge. Versioned: changing a word changes the judge version.

Verdicts (docs/plans/llm-judge.md). The "supported" prompt never sees the gold answer; the
"responsive" prompt does. Each is answered through one strict tool call, which the prompt asks
for, so the verdict is schema-checked, never parsed out of prose.

V2 of "supported" (2026-10-12) returns two levels instead of one. Validation against 114 hand
labels showed the single every-claim verdict failing 72% of the answers a careful reader passed:
the fact that answered the question was quoted, and added context was not. So the judge now
says whether the *core* claims are supported and, separately, whether *every* claim is. The
rubric below is the same text the labeling screen shows the human, so both apply one standard.
Rubric revised 2026-10-16 (Aleena): the headerless-row rule applies to rows as well as cells, and
a header fragment quoted alongside the value is the one thing that lifts it.
"""

from __future__ import annotations

from typing import Any

SUPPORT_RUBRIC = """\
Core claims are the statements that answer the question: the direct answer and the facts it
rests on. Peripheral claims are everything else factual: added context, extra numbers, names
of APIs or parameters, descriptions of other features, statements about what the documentation
says or where.

A claim is supported when a quote states it, or when it follows from the quotes by simple
arithmetic, a unit conversion (900 seconds is 15 minutes), or plain rewording. A table row or cell
quoted without its column header supports only what it literally says: a bare "Yes" does not
establish "adjustable", and a row of dates does not establish which date is the deprecation date.
The header counts only when it is inside a quote: a quoted header fragment that names the column
(for example "Deprecation date") does establish the meaning of that value. Advice ("check the
console"), hedges ("I could not confirm"), and offers to help are not factual claims and need no
support. Judge support, not truth: a true claim that no quote states is unsupported."""

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

# V3 of the rubric (candidate, 2026-10-16): adds the quota-row example that the judge-test
# validation showed the judge reading differently from the labeler. Validated only against labels
# on a new run's answers; until then the judge built on it runs under another grader name.
SUPPORT_RUBRIC_V3 = (
    SUPPORT_RUBRIC
    + """

A quota row quoted without its header, such as "Cache behaviors per distribution | 75", states
the number 75 and nothing else: not that 75 is a default, a maximum or a limit, and not whether
it can be raised. Those are claims about the column, which only a quoted header can supply.
Billing factors, commitment terms and option lists are core claims when the question asks how
something is billed or what the options are; otherwise they are peripheral."""
)

# V4 of the rubric (DRAFT, 2026-10-16): rubric v2 plus Aleena's two decisions after the v2-vs-v3
# adjudication (docs/judge-validation.md section 7). (1) Decided and written: a statement that the
# documentation does NOT say something is a hedge. (2) Decided, wording to be added by Aleena:
# when a question asks how something is billed, the price factors are not part of the main
# answer (they are peripheral). Not validated and not used by any gate until (2) is written and
# the judge built on it is scored against fresh labels; v2 stays the validated judge until then.
SUPPORT_RUBRIC_V4 = (
    SUPPORT_RUBRIC
    + """

Statements about what the documentation says or where are claims, but a statement that it does
not say something ("the docs don't state the starting number", "the docs don't define a new
customer") is a hedge and needs no support."""
)

SUPPORT_RUBRICS = {"v2": SUPPORT_RUBRIC, "v3": SUPPORT_RUBRIC_V3, "v4": SUPPORT_RUBRIC_V4}


def supported_prompt(rubric: str) -> str:
    """The two-level support prompt built around one rubric text."""
    return f"""\
You are checking whether an answer is supported by the evidence it cites.

You will be given a user's question about AWS documentation, an assistant's answer, and the
quotes the assistant cited from the documentation. Decide two things, both about support only:

1. core_supported: is every core claim supported by the cited quotes?
2. fully_supported: is every claim in the answer, core and peripheral, supported by them?

{rubric}

Do not judge whether the answer is correct, complete or helpful. That is someone else's job.
fully_supported can only be "pass" when core_supported is "pass".

Call submit_support_verdict with both verdicts and a one-sentence reason that names the first
unsupported claim you found, or says that every claim is quoted."""


JUDGE_SUPPORTED_V2 = supported_prompt(SUPPORT_RUBRIC)

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

_PASS_FAIL = {"type": "string", "enum": ["pass", "fail"]}


def verdict_tool(strict: bool) -> dict[str, Any]:
    """One pass/fail verdict with a reason (used by "responsive")."""
    spec: dict[str, Any] = {
        "name": "submit_verdict",
        "description": "Record the verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": _PASS_FAIL,
                "reason": {"type": "string", "description": "One sentence."},
            },
            "required": ["verdict", "reason"],
            "additionalProperties": False,
        },
    }
    if strict:
        spec["strict"] = True
    return spec


def support_verdict_tool(strict: bool) -> dict[str, Any]:
    """Two-level support verdict: core claims, then every claim."""
    spec: dict[str, Any] = {
        "name": "submit_support_verdict",
        "description": "Record whether the core claims, and then every claim, are supported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "core_supported": _PASS_FAIL,
                "fully_supported": _PASS_FAIL,
                "reason": {"type": "string", "description": "One sentence."},
            },
            "required": ["core_supported", "fully_supported", "reason"],
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
