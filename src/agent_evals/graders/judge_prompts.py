"""Model-facing text for the LLM judge. Versioned: changing a word changes the judge version.

Verdicts (docs/plans/llm-judge.md). The "supported" prompt never sees the gold answer; the
"responsive" prompt does. Each is answered through one strict tool call, which the prompt asks
for, so the verdict is schema-checked, never parsed out of prose.

V2 of "supported" (2026-10-12) returns two levels instead of one. Validation against 114 hand
labels showed the single every-claim verdict failing 72% of the answers a careful reader passed:
the fact that answered the question was quoted, and added context was not. So the judge now
says whether the *core* claims are supported and, separately, whether *every* claim is. The
rubric below is the same text the labeling screen shows the human, so both apply one standard.
Rubric revised 2026-10-16: the headerless-row rule applies to rows as well as cells, and
a header fragment quoted alongside the value is the one thing that lifts it.
"""

from __future__ import annotations

import json
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

# V4 of the rubric (candidate, wording final 2026-10-17): rubric v2 plus the two decisions made
# after the v2-vs-v3 adjudication (docs/judge-validation.md section 7): (1) a statement that the
# documentation does NOT say something is a hedge; (2) when a question asks how something is
# billed, the price factors are peripheral, not part of the main answer (wording approved
# 2026-10-17). Not used by any gate until the judge built on it passes the rule in
# docs/judge-validation.md section 8; v2 stays the validated judge until then.
SUPPORT_RUBRIC_V4 = (
    SUPPORT_RUBRIC
    + """

Statements about what the documentation says or where are claims, but a statement that it does
not say something ("the docs don't state the starting number", "the docs don't define a new
customer") is a hedge and needs no support.

When a question asks how something is billed, the core claims are how and when you are charged
(for example, billed hourly until you delete it). The factors that set the price, such as the
model, the number of units bought or the length of a commitment, are peripheral."""
)

# V2.1 of the rubric (candidate, 2026-10-17): rubric v2 plus one paragraph on reporting phrases.
# The 30-answer spot check of prompt v5's answers found the judge passing two the labeler failed,
# both attributions credited from the cited URL ("according to the cross-Region inference
# documentation"; "stated in the v2 user guide, so it applies to v2"), while the earlier labels
# pass plain reporting frames. The paragraph states that line; two isolated reviewers checked it
# against all 224 labels before it was fixed (docs/judge-validation.md section 9). Not used by any
# gate unless it passes the rule committed there.
SUPPORT_RUBRIC_V2_1 = (
    SUPPORT_RUBRIC
    + """

Reporting what the documentation says ("the documentation says ...", "according to the Amazon
Bedrock docs, ...", "AWS recommends ...") needs no support beyond a quote of what it reports.
The publisher (AWS, or the "we" of a quote), the service's own name in any form (Bedrock or
Amazon Bedrock, KMS or AWS KMS, SageMaker or SageMaker AI) and generic words for a kind of source
(documentation, docs, page, guide, quotas page, table, comparison table) add nothing. But a
feature, version or edition name that such a phrase introduces, and that neither the question nor
a quote uses, is an unsupported peripheral claim: "according to the cross-Region inference
documentation" is unsupported when no quote mentions cross-Region inference, and "this is stated
in the v2 user guide, so it applies to v2" is unsupported when no quote names v2. The question
can clear only the source a phrase names; what the phrase reports must still be in a quote. That
the documentation points to or links to something else (a pricing page, a blog post) is reported
content and needs a quote of its own. A citation's URL and the page text around a quote are not
quotes. This paragraph applies only to names introduced by a reporting phrase."""
)

# V5 of the rubric (the claim judge's, approved 2026-10-28: docs/plans/judge-v5.md, decision 2):
# rubric v4 plus two paragraphs. The first reuses rubric v2.1's line on reporting phrases but
# keeps a statement that the documentation does not say something a hedge, the line v2.1
# crossed; the second writes down that the labels pass advice that names a command or a
# parameter. Both examples are from dev-split answers (docs-ai-001-p2, docs-chg-004).
SUPPORT_RUBRIC_V5 = (
    SUPPORT_RUBRIC_V4
    + """

A phrase that reports the documentation ("the documentation says", "according to the Amazon
Bedrock docs", "AWS recommends") is not a claim by itself; what it reports is. But a feature,
version or edition name that such a phrase introduces, and that neither the question nor any
quote uses, is a peripheral claim of its own: "according to the cross-Region inference
documentation" is unsupported when no quote mentions cross-Region inference. A citation's URL
and the page text around a quote are not quotes. This paragraph is about reporting phrases
only: a statement that the documentation does not say something, or that a page could not be
opened, is a hedge under the rule above.

Advice stays advice when it names an API, a command, a parameter, a metric or a tool ("check
your account's actual value in the Service Quotas console, or with aws lambda
get-account-settings"); only a statement of fact about one of them is a claim."""
)

SUPPORT_RUBRICS = {
    "v2": SUPPORT_RUBRIC,
    "v2.1": SUPPORT_RUBRIC_V2_1,
    "v3": SUPPORT_RUBRIC_V3,
    "v4": SUPPORT_RUBRIC_V4,
    "v5": SUPPORT_RUBRIC_V5,
}


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


# The claim judge (judge v5, docs/plans/judge-v5.md). One call returns a table of the answer's
# claims that code checks; a second, skeptical call scores what code flagged and looks for a
# claim the table missed. Neither call sees a URL (a URL is not a quote, and it is how
# attributions were credited): not a citation's, and not one in a quote's text, which the claim
# judge takes out before the quotes reach these templates. Neither prompt has an example beyond
# the rubric's own, and neither mentions a threshold, so one reply serves every threshold.
# Changing a word of the prompts, the tools or the message templates below changes the claim
# judge's version.


def claims_prompt(rubric: str) -> str:
    """The claim-table prompt built around one rubric text."""
    return f"""\
You are checking whether an answer is supported by the evidence it cites.

You will be given a user's question about AWS documentation, an assistant's answer, and the
numbered quotes the assistant cited from the documentation. List the answer's factual claims,
and for each one the quotes that support it and how sure you are of that support.

{rubric}

Advice, hedges (a statement that the documentation does not say something among them) and offers
to help make no claim at all.

Give one entry per claim; a sentence that states several facts is several claims:
- answer_span: the words of the answer that make the claim, copied exactly.
- role: "core" or "peripheral", as defined above.
- support: each quote that supports the claim, by its number, with quote_span, the words of that
  quote that state it, copied exactly and at least three words long. Leave it empty when no quote
  supports the claim.
- score: from 0 to 100, how sure you are that the cited quotes support the claim under the rules
  above. Supported keeps its meaning: a quote states the claim, or it follows from the quotes by
  simple arithmetic, a unit conversion or plain rewording. The score says how sure you are of
  that, not whether the claim is true.
- why: one short sentence.

Copy the words of the answer that make no claim into not_claims. Every number in the answer, and
every capitalized name that the question does not use, must fall inside a claim's answer_span or
a not_claims entry.

Do not judge whether the answer is correct, complete or helpful. That is someone else's job.

Call submit_claims with the table."""


def skeptic_prompt(rubric: str) -> str:
    """The skeptic's prompt built around the same rubric text."""
    return f"""\
You are checking an answer for claims that the evidence it cites does not support.

You will be given a user's question about AWS documentation, an assistant's answer, the numbered
quotes the assistant cited from the documentation, a table of the answer's claims made by an
earlier check, and a numbered list of flagged items: numbers and names in the answer that the
table left out, listed as making no claim, or did not find in the quotes it cited for them. The
earlier check's usual mistake is to pass a claim that no quote supports.

{rubric}

Advice, hedges (a statement that the documentation does not say something among them) and offers
to help make no claim at all.

Do two things.

1. For each flagged item, give a score from 0 to 100: how sure you are that the cited quotes
   support the item, as the words of the answer around it state it, under the rules above. The
   score measures support only, so an item that no quote supports scores low even when those
   words make no claim. Set not_a_claim to true when those words make no claim at all. That
   clears a name, but never a number: a number passes only on its score. Say why in one short
   sentence.

2. Search the whole answer for the one claim most likely to be unsupported, including any the
   table missed. Look especially at statements about a named source; terms no quote states, such
   as features, versions, editions, Regions and pricing models; strengthened scope or qualifiers
   ("only", "always", "most", "up to", "by default"); numbers and units; and table cells read as
   a default or a maximum when no quoted header says so. Give the claim's words from the answer,
   copied exactly, its role ("core" or "peripheral", as defined above), why no quote states it in
   one short sentence, and from 0 to 100 how sure you are that it is unsupported. If you find no
   claim that may be unsupported, set found to false and leave answer_span empty.

Do not judge whether the answer is correct, complete or helpful. That is someone else's job.

Call submit_skeptic with a result for every flagged item, by its number, and the search."""


_ROLE = {"type": "string", "enum": ["core", "peripheral"]}


def claims_tool(strict: bool) -> dict[str, Any]:
    """The claim table: each claim with its role, the quote spans that support it and a 0 to 100
    score, and the words of the answer that make no claim. Code checks the scores' range, since
    a strict schema cannot carry a minimum or a maximum."""
    support = {
        "type": "object",
        "properties": {
            "quote": {"type": "integer", "description": "The quote's number."},
            "quote_span": {
                "type": "string",
                "description": "The words of that quote that state the claim, copied exactly.",
            },
        },
        "required": ["quote", "quote_span"],
        "additionalProperties": False,
    }
    claim = {
        "type": "object",
        "properties": {
            "answer_span": {
                "type": "string",
                "description": "The words of the answer that make the claim, copied exactly.",
            },
            "role": _ROLE,
            "support": {"type": "array", "items": support},
            "score": {
                "type": "integer",
                "description": "0 to 100: how sure you are that the cited quotes support it.",
            },
            "why": {"type": "string", "description": "One short sentence."},
        },
        "required": ["answer_span", "role", "support", "score", "why"],
        "additionalProperties": False,
    }
    spec: dict[str, Any] = {
        "name": "submit_claims",
        "description": "Record the answer's claims and the words of it that make no claim.",
        "input_schema": {
            "type": "object",
            "properties": {
                "claims": {"type": "array", "items": claim},
                "not_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Words of the answer that make no claim, copied exactly.",
                },
            },
            "required": ["claims", "not_claims"],
            "additionalProperties": False,
        },
    }
    if strict:
        spec["strict"] = True
    return spec


def skeptic_tool(strict: bool) -> dict[str, Any]:
    """The skeptic's reply: a 0 to 100 support score for every flagged item, keyed by the item's
    number, and the one claim most likely unsupported, or none."""
    item = {
        "type": "object",
        "properties": {
            "item": {"type": "integer", "description": "The flagged item's number."},
            "score": {
                "type": "integer",
                "description": "0 to 100: how sure you are that the cited quotes support it.",
            },
            "not_a_claim": {
                "type": "boolean",
                "description": (
                    "True when the words around it make no claim at all. It clears a name, "
                    "never a number."
                ),
            },
            "why": {"type": "string", "description": "One short sentence."},
        },
        "required": ["item", "score", "not_a_claim", "why"],
        "additionalProperties": False,
    }
    search = {
        "type": "object",
        "properties": {
            "found": {"type": "boolean", "description": "False when no claim may be unsupported."},
            "answer_span": {
                "type": "string",
                "description": "The words of the answer that make the claim, copied exactly.",
            },
            "role": _ROLE,
            "why": {"type": "string", "description": "Why no quote states it, in one sentence."},
            "confidence": {
                "type": "integer",
                "description": "0 to 100: how sure you are that it is unsupported.",
            },
        },
        "required": ["found", "answer_span", "role", "why", "confidence"],
        "additionalProperties": False,
    }
    spec: dict[str, Any] = {
        "name": "submit_skeptic",
        "description": "Record each flagged item's score and the claim most likely unsupported.",
        "input_schema": {
            "type": "object",
            "properties": {"flagged": {"type": "array", "items": item}, "search": search},
            "required": ["flagged", "search"],
            "additionalProperties": False,
        },
    }
    if strict:
        spec["strict"] = True
    return spec


# The claim judge's message templates. The quotes are numbered from 1 and shown without URLs.
QUOTE_LINE = '{number}. "{quote}"'
CLAIMS_MESSAGE = """\
Question:
{question}

Assistant's answer:
{answer}

Cited quotes:
{quotes}"""
SKEPTIC_MESSAGE = (
    CLAIMS_MESSAGE
    + """

The earlier check's claim table:
{table}

Flagged items:
{items}"""
)
ITEM_LINE = '{number}. "{token}" in "{context}": {flag}'
FLAGS = {  # why code flagged an item, by its kind
    "uncovered": "no claim in the table covers it",
    "not_claims": "the table lists these words as making no claim",
    "number_not_quoted": "claim {claim} states it, but none of that claim's quote spans does",
}
NO_ITEMS = "none"


def numbered_quotes(quotes: list[str]) -> str:
    return "\n".join(QUOTE_LINE.format(number=n, quote=q) for n, q in enumerate(quotes, 1))


def claims_message(question: str, answer: str, quotes: list[str]) -> str:
    return CLAIMS_MESSAGE.format(question=question, answer=answer, quotes=numbered_quotes(quotes))


def skeptic_message(
    question: str,
    answer: str,
    quotes: list[str],
    table: dict[str, Any],
    items: list[dict[str, Any]],
) -> str:
    """The claim message plus the claim table as code kept it and the flagged items, each given
    as its number, token, context, kind and claim. It never holds a threshold."""
    lines = [
        ITEM_LINE.format(
            number=item["number"],
            token=item["token"],
            context=item["context"],
            flag=FLAGS[item["kind"]].format(claim=item["claim"]),
        )
        for item in items
    ]
    return SKEPTIC_MESSAGE.format(
        question=question,
        answer=answer,
        quotes=numbered_quotes(quotes),
        table=json.dumps(table, indent=2, ensure_ascii=False),
        items="\n".join(lines) or NO_ITEMS,
    )
