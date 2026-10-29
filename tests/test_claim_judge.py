"""The claim judge (docs/plans/judge-v5.md): a claim table that code checks, then a skeptic."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.config import JudgeConfig, load_config
from agent_evals.graders import claim_judge as cj
from agent_evals.graders.claim_judge import (
    Assessment,
    CheckedClaim,
    CheckedSupport,
    ClaimJudgeGrader,
    Search,
    SkepticReply,
    normalize,
)
from agent_evals.graders.judge import JudgeGrader
from agent_evals.graders.judge_prompts import SUPPORT_RUBRICS
from agent_evals.harness import llm as llm_module
from agent_evals.harness.llm import LLMResponse
from agent_evals.harness.manifest import file_sha256
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.store.db import ResultsStore
from agent_evals.suites import write_suite
from agent_evals.types import AgentResult
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use, write_pilot

ROOT = Path(__file__).resolve().parents[1]
# Typography a copy can change, spelled by code point so that no lookalike sits in the source.
LSQUO, RSQUO, LDQUO, RDQUO, NDASH, MDASH, NBSP = map(
    chr, (0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x00A0)
)

QUOTE = "Bucket names must be between 3 and 63 characters long."
ANSWER = "Bucket names must be between 3 and 63 characters long."
SPAN = "between 3 and 63 characters long"
# One uncovered name, and a number and two names among the words of no claim.
FLAGGED_ANSWER = (
    "Bucket names must be between 3 and 63 characters long. They are unique across every Region "
    "by default, as I read it. You can ask for up to 1,000 buckets in the Service Quotas console."
)
NO_CLAIM = "You can ask for up to 1,000 buckets in the Service Quotas console."


def v5(**overrides: Any) -> JudgeConfig:
    """The claim judge's settings as configs/judge-v5.yaml has them, with overrides."""
    base = {
        "method": "claims",
        "grader_name": "judge-v5",
        "rubric_version": "v5",
        "responsive": False,
        "model": {"name": "claude-sonnet-5-5", "max_tokens": 8192},
    }
    return JudgeConfig.model_validate(base | overrides)


def grader(llm: ScriptedLLM | None = None, **overrides: Any) -> ClaimJudgeGrader:
    return ClaimJudgeGrader(llm or ScriptedLLM([]), v5(**overrides))


def answered(answer: str = ANSWER, *quotes: str) -> AgentResult:
    citations = [{"url": BUCKETS_URL, "quote": q} for q in quotes or (QUOTE,)]
    return AgentResult(output={"answer": answer, "citations": citations, "abstained": False})


def claim(
    span: str, *support: tuple[int, str], role: str = "core", score: int = 90
) -> dict[str, Any]:
    return {
        "answer_span": span,
        "role": role,
        "support": [{"quote": q, "quote_span": s} for q, s in support],
        "score": score,
        "why": f"scored {score}",
    }


QUOTED = claim(SPAN, (1, SPAN))


def table(*claims: dict[str, Any], not_claims: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [tool_use("submit_claims", {"claims": list(claims), "not_claims": list(not_claims)})]


def skeptic(
    scores: dict[int, int] | None = None,
    *,
    no_claim: tuple[int, ...] = (),
    span: str = "",
    role: str = "peripheral",
    confidence: int = 0,
) -> list[dict[str, Any]]:
    flagged = [
        {"item": n, "score": score, "not_a_claim": n in no_claim, "why": f"item {n}"}
        for n, score in (scores or {}).items()
    ]
    search = {
        "found": bool(span),
        "answer_span": span,
        "role": role,
        "why": "no quote says so",
        "confidence": confidence,
    }
    return [tool_use("submit_skeptic", {"flagged": flagged, "search": search})]


class Stops(ScriptedLLM):
    """ScriptedLLM whose replies end with the given stop reasons, in order."""

    def __init__(self, script: list[list[dict[str, Any]]], stops: list[str]) -> None:
        super().__init__(script)
        self.stops = list(stops)

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        reply = super().create(request, salt=salt)
        return reply.model_copy(update={"stop_reason": self.stops.pop(0)})


def assessed(llm: ScriptedLLM, answer: str = ANSWER, *quotes: str, **config: Any) -> Assessment:
    assessment = grader(llm, **config).assess(make_case(), answered(answer, *quotes))
    assert assessment is not None
    return assessment


def test_normalize_folds_what_a_copy_can_change() -> None:
    copied = f"  Bucket{NBSP}names {LSQUO}must{RSQUO} be {LDQUO}short{RDQUO} {NDASH} or {MDASH} "
    assert (
        normalize(copied + "`long` | 63 |\n\n") == "bucket names 'must' be \"short\" - or - long 63"
    )
    assert normalize("FILE " + chr(0xFB01) + "le " + chr(0xFF13)) == "file file 3"  # NFKC
    assert normalize("STRASSE") == normalize("Stra" + chr(0xDF) + "e")  # casefold, not lower


def test_a_quoted_answer_passes_on_a_table_and_a_skeptic_call() -> None:
    llm = ScriptedLLM([table(QUOTED), skeptic()])
    by = {g.metric: g for g in grader(llm).grade(make_case(), answered())}
    assert by["judge_supported"].passed and by["judge_core_supported"].passed
    assert by["judge_supported"].value == 1.0 and by["judge_supported"].reason == (
        "every claim is quoted"
    )
    assert by["judge_support_score"].value == 0.9 and by["judge_support_score"].passed is None
    assert set(by) == {"judge_supported", "judge_core_supported", "judge_support_score"}
    assert llm.salts == ["judge#claims#t-1", "judge#skeptic#t-1"]
    claims_request, skeptic_request = llm.requests
    assert claims_request["tools"][0]["name"] == "submit_claims"
    assert skeptic_request["tools"][0]["name"] == "submit_skeptic"
    assert claims_request["model"] == "claude-sonnet-5-5" and claims_request["max_tokens"] == 8192
    assert "tool_choice" not in claims_request and "tool_choice" not in skeptic_request


def test_errored_abstained_and_uncited_answers_are_not_judged() -> None:
    llm = ScriptedLLM([])
    judge = grader(llm)
    assert judge.assess(make_case(), AgentResult(error="max_steps")) is None
    declined = AgentResult(output={"answer": "", "citations": [], "abstained": True})
    assert judge.grade(make_case(), declined) == []
    uncited = AgentResult(output={"answer": "63 characters", "citations": [], "abstained": False})
    assert judge.grade(make_case(), uncited) == []
    assert llm.requests == []


def test_the_judge_sees_numbered_quotes_and_never_a_url_or_the_gold() -> None:
    second = "Bucket names can contain only lowercase letters, numbers, dots, hyphens."
    llm = ScriptedLLM([table(QUOTED), skeptic()])
    grader(llm).grade(make_case(), answered(ANSWER, QUOTE, second))
    claims_message, skeptic_message = (r["messages"][0]["content"] for r in llm.requests)
    assert f'1. "{QUOTE}"' in claims_message and f'2. "{second}"' in claims_message
    for message in (claims_message, skeptic_message):
        assert BUCKETS_URL not in message and "http" not in message
        assert "3 to 63 characters" not in message  # the gold answer
    assert "The earlier check's claim table:" in skeptic_message
    assert skeptic_message.endswith("Flagged items:\nnone")


def test_spans_match_through_typography() -> None:
    quote = "| Function timeout | 900 seconds (15 minutes) |"
    answer = f"Set `timeout` to at most 900 seconds {MDASH} that is, 15 minutes."
    span = "Set timeout to at most 900 seconds - that is, 15 minutes"
    timeout = claim(span, (1, "Function timeout 900 seconds (15 minutes)"))
    found = assessed(ScriptedLLM([table(timeout), skeptic()]), answer, quote)
    assert found.error is None and found.dropped_support == 0 and found.flagged == []
    assert found.verdict(70, 90).fully_supported


def test_normalize_drops_asterisks_and_makes_every_dash_a_hyphen() -> None:
    assert normalize("between **3** and *63* characters") == "between 3 and 63 characters"
    for dash in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212):
        assert normalize(f"us{chr(dash)}east{chr(dash)}1") == "us-east-1", hex(dash)
    bold = "Bucket names must be between **3** and **63** characters long."
    found = assessed(ScriptedLLM([table(QUOTED), skeptic()]), ANSWER, bold)
    assert found.dropped_support == 0 and found.verdict(85, 90).fully_supported


def test_spans_match_only_whole_words_and_numbers() -> None:
    answer = "Each object can hold up to 5 GB per object."
    five = claim("up to 5 GB per object", (1, "5 GB per object"))
    inside = ScriptedLLM([table(five), skeptic({1: 90})])
    found = assessed(inside, answer, "The limit is 15 GB per object.")
    assert [s.problem for s in found.claims[0].support] == ["not in quote 1"]
    assert [(i.kind, i.token) for i in found.flagged] == [("number_not_quoted", "5")]
    assert not found.verdict(50, None).fully_supported  # no quote span of it holds
    whole = assessed(ScriptedLLM([table(five), skeptic()]), answer, "The limit is 5 GB per object.")
    assert whole.dropped_support == 0 and whole.flagged == []
    assert whole.verdict(85, 90).fully_supported
    cut = claim("ach object can hold up to 5 GB", (1, "The limit is 5 GB"))  # starts in a word
    llm = ScriptedLLM([table(cut), table(five), skeptic()])
    assert assessed(llm, answer, "The limit is 5 GB per object.").retries == 1
    assert llm.salts[1] == "judge#claims-retry#t-1"


def test_an_empty_span_is_never_found() -> None:
    assert not cj._occurs("", normalize(ANSWER)) and not cj._occurs(" `` ", normalize(ANSWER))
    empty = claim("", (1, SPAN))
    llm = ScriptedLLM([table(empty), table(QUOTED, not_claims=("",)), skeptic()])
    found = assessed(llm)
    assert found.retries == 1 and found.error is None  # the empty answer_span was refused
    assert found.unmatched_not_claims == 1  # an empty not_claims entry covers nothing
    assert found.verdict(70, 90).fully_supported
    search = {"found": True, "answer_span": "", "role": "core", "why": "no", "confidence": 95}
    names_nothing = [tool_use("submit_skeptic", {"flagged": [], "search": search})]
    found = assessed(ScriptedLLM([table(QUOTED), names_nothing, names_nothing]))
    assert found.search_span_missing and found.retries == 1
    assert found.verdict(70, 70).fully_supported  # read as none found


def test_link_targets_and_bare_urls_are_taken_out_of_quotes() -> None:
    linked = (
        "Bucket names must be between 3 and 63 characters long, as the "
        "[naming rules](https://docs.example.test/s3/naming.html) say. "
        "See https://docs.example.test/s3/limits.html, <https://docs.example.test/s3/> "
        "and [the quotas](./quotas.html) for www.example.test/s3."
    )
    shown = (
        "Bucket names must be between 3 and 63 characters long, as the naming rules say. "
        "See ,  and the quotas for ."
    )
    assert cj.without_links(linked) == shown
    assert cj.without_links("[Step 2: [Optional] Pick](https://x.test/a_(b)) ![a map](m.png)") == (
        "Step 2: [Optional] Pick a map"
    )
    entries = claim(
        SPAN,
        (1, SPAN),
        (1, "as the naming rules say"),
        (1, "naming rules](https://docs.example.test/s3/naming.html) say"),
    )
    llm = ScriptedLLM([table(entries), skeptic()])
    found = assessed(llm, ANSWER, linked)
    assert [s.problem for s in found.claims[0].support] == [None, None, "not in quote 1"]
    claims_message, skeptic_message = (r["messages"][0]["content"] for r in llm.requests)
    assert f'1. "{shown}"' in claims_message
    for message in (claims_message, skeptic_message):
        assert "http" not in message and "example.test" not in message and "](" not in message


def test_a_support_entry_that_fails_its_check_is_dropped_and_counted() -> None:
    second = "Bucket names can contain only lowercase letters, numbers, dots, hyphens."
    answer = ANSWER + " They can contain only lowercase letters, numbers, dots, hyphens."
    rules = claim(
        "They can contain only lowercase letters, numbers, dots, hyphens",
        (2, "contain only uppercase letters"),  # not in quote 2
        (3, "only lowercase letters, numbers"),  # there is no quote 3
        role="peripheral",
    )
    length = claim(SPAN, (1, SPAN), (1, "63 characters"))  # too short, but one entry still holds
    llm = ScriptedLLM([table(length, rules)])
    found = assessed(llm, answer, QUOTE, second)
    assert found.dropped_support == 3 and found.flagged == []
    assert [s.problem for s in found.claims[0].support] == [None, "shorter than 3 words"]
    assert [s.problem for s in found.claims[1].support] == ["not in quote 2", "there is no quote 3"]
    v = found.verdict(70, 90)
    assert (
        not v.fully_supported and v.core_supported
    )  # the claim left without support is peripheral
    assert v.reason.startswith('"They can contain') and "no quote span of it holds" in v.reason
    assert len(llm.requests) == 1  # it fails at t = 50 with nothing flagged: no skeptic call


def test_coverage_flags_uncovered_names_and_numbers_and_names_among_words_of_no_claim() -> None:
    llm = ScriptedLLM(
        [
            table(QUOTED, not_claims=(NO_CLAIM,)),
            skeptic({1: 80, 2: 90, 3: 5, 4: 5}, no_claim=(3, 4)),
        ]
    )
    found = assessed(llm, FLAGGED_ANSWER)
    assert [(i.number, i.kind, i.token) for i in found.flagged] == [
        (1, "uncovered", "Region"),  # "I", and the words that start sentences, are not flagged
        (2, "not_claims", "1,000"),
        (3, "not_claims", "Service"),
        (4, "not_claims", "Quotas"),
    ]
    assert (
        found.flagged[0].context == "They are unique across every Region by default, as I read it."
    )
    assert {i.context for i in found.flagged[1:]} == {NO_CLAIM}
    items = llm.requests[1]["messages"][0]["content"].split("Flagged items:\n")[1].splitlines()
    assert items[0] == (
        '1. "Region" in "They are unique across every Region by default, as I read it.": no claim '
        "in the table covers it"
    )
    assert items[1].startswith('2. "1,000" in "You can ask') and items[1].endswith(
        "the table lists these words as making no claim"
    )
    assert found.verdict(70, 90).fully_supported  # each item scored at t, or a name of no claim
    at_85 = found.verdict(85, 90)  # "Region" scored 80: an uncovered token fails fully only
    assert not at_85.fully_supported and at_85.core_supported
    assert at_85.reason.startswith('"Region" in no claim, skeptic score 80')
    assert '; flagged: "Region" in no claim, "1,000" in words of no claim' in at_85.reason


def test_a_number_among_words_of_no_claim_must_be_scored_at_t() -> None:
    llm = ScriptedLLM(
        [
            table(QUOTED, not_claims=(NO_CLAIM,)),
            skeptic({1: 80, 2: 20, 3: 5, 4: 5}, no_claim=(2, 3, 4)),
        ]
    )
    v = assessed(llm, FLAGGED_ANSWER).verdict(70, None)
    assert not v.fully_supported and v.core_supported  # "no claim" does not clear a number
    assert v.reason.startswith('"1,000" in words of no claim, skeptic score 20')


def test_coverage_skips_list_markers_and_names_the_question_uses() -> None:
    case = make_case(question="Which Region does an Amazon bucket live in?")
    answer = (
        "Each Amazon bucket lives in one Region.\n"
        "1. Pick it when you create the bucket.\n"
        "- Buckets stay there, I believe, unlike CloudFront."
    )
    llm = ScriptedLLM([table(), skeptic({1: 90})])
    found = grader(llm).assess(case, answered(answer))
    assert found is not None and [(i.kind, i.token) for i in found.flagged] == [
        ("uncovered", "CloudFront")
    ]
    assert found.verdict(70, 90).support_score == 1.0  # no claims


def test_a_number_starts_with_a_digit_and_a_name_with_digits_is_a_name() -> None:
    advice = "You can check them in the EC2 console over IPv6."
    answer = "Bucket names must be between 3 and 63 characters long in S3. " + advice
    in_s3 = claim("between 3 and 63 characters long in S3", (1, SPAN))  # no quote span says S3
    llm = ScriptedLLM([table(in_s3, not_claims=(advice,)), skeptic({1: 0, 2: 0}, no_claim=(1, 2))])
    found = assessed(llm, answer)
    assert [(i.kind, i.token) for i in found.flagged] == [
        ("not_claims", "EC2"),
        ("not_claims", "IPv6"),
    ]
    assert found.verdict(85, 90).fully_supported  # names in words of no claim, though scored 0
    words = ("3", "1,000", "15GB", "2026-10-28", "EC2", "S3", "IPv6", "v2")
    assert [cj._is_number(w) for w in words] == [True] * 4 + [False] * 4


def test_abbreviations_and_hard_wrapped_lines_start_no_sentence() -> None:
    answer = (
        "Move old objects to a colder class, e.g. Glacier.\n"
        "The discount applies to\n"
        "Reserved Instances only.\n"
        "* Lifecycle rules move them.\n"
        "Transitions take a day."
    )
    llm = ScriptedLLM([table(), skeptic({1: 90, 2: 90, 3: 90})])
    found = grader(llm).assess(make_case(), answered(answer))
    assert found is not None and [(i.token, i.context) for i in found.flagged] == [
        ("Glacier", "Move old objects to a colder class, e.g. Glacier."),
        ("Reserved", "The discount applies to Reserved Instances only."),
        ("Instances", "The discount applies to Reserved Instances only."),
    ]
    starts = cj._words("Pick S3 vs. EBS (or EFS, etc.) for it. Then, i.e. Next, done.")
    assert [w.text for w in starts if w.first] == ["Pick", "Then"]


def test_a_sentence_starts_after_a_blank_line_and_at_a_list_item_heading_or_table_row() -> None:
    text = (
        "Intro without a period\n\n"
        "After a blank line\n"
        "## Heading\n"
        "Under it\n"
        "| Standard | 3 |\n"
        "| Glacier | 90 |\n"
        "- First item\n"
        "2. Second item\n"
        "* Third item\n"
        "**4.** Fourth item\n"
        "Wrapped on"
    )
    starts = [w.text for w in cj._words(text) if w.first]
    assert starts == [
        "Intro",
        "After",
        "Heading",
        "Under",
        "Standard",
        "Glacier",
        "First",
        "Second",
        "Third",
        "Fourth",
    ]
    assert cj._fold(text).split("\n")[4] == "Standard 3"  # what the words are placed in
    assert cj._fold(text).split("\n")[8] == "Third item"  # its "*" read before folding drops it


CROSS_CHECK = [  # answer, claim span, quote, quote span, the number no quote span holds whole
    (
        "A session lasts 1 hour at most.",
        "lasts 1 hour at most",
        "A session lasts 1.5 hours at most.",
        "lasts 1.5 hours at most",
        "1",
    ),
    (
        "Objects are 99% durable here.",
        "Objects are 99% durable",
        "Objects are 99.9% durable here.",
        "Objects are 99.9% durable",
        "99",
    ),
    (
        "Each item holds 2 MB of data.",
        "holds 2 MB of data",
        "Each item holds 2,048 MB of data.",
        "holds 2,048 MB of data",
        "2",
    ),
    (
        "Keep 3 copies of each object.",
        "Keep 3 copies of each object",
        "Keep copies of each object in S3.",
        "copies of each object in S3",
        "3",
    ),
]


@pytest.mark.parametrize(("answer", "span", "quote", "quote_span", "token"), CROSS_CHECK)
def test_the_number_cross_check_respects_decimal_and_thousands_boundaries(
    answer: str, span: str, quote: str, quote_span: str, token: str
) -> None:
    stated = claim(span, (1, quote_span))
    found = assessed(ScriptedLLM([table(stated), skeptic({1: 90})]), answer, quote)
    assert found.dropped_support == 0
    assert [(i.kind, i.token) for i in found.flagged] == [("number_not_quoted", token)]
    same = assessed(ScriptedLLM([table(claim(span, (1, span))), skeptic()]), answer, answer)
    assert same.flagged == []


def test_a_number_only_in_a_dropped_quote_span_is_flagged() -> None:
    length = claim(SPAN, (1, "Bucket names must be"), (1, "between 3 and 63 characters short"))
    found = assessed(ScriptedLLM([table(length), skeptic({1: 90, 2: 90})]))
    assert [s.problem for s in found.claims[0].support] == [None, "not in quote 1"]
    assert [(i.kind, i.token, i.claim) for i in found.flagged] == [
        ("number_not_quoted", "3", 1),
        ("number_not_quoted", "63", 1),
    ]


def test_a_claims_number_missing_from_its_quote_spans_is_flagged_and_bears_on_core() -> None:
    answer, quote = "A function can run for up to 15 minutes.", "Function timeout 900 seconds"
    timeout = claim("can run for up to 15 minutes", (1, "Function timeout 900 seconds"))
    llm = ScriptedLLM([table(timeout), skeptic({1: 95})])
    found = assessed(llm, answer, quote)
    [item] = found.flagged
    assert (item.kind, item.token, item.claim, item.core) == ("number_not_quoted", "15", 1, True)
    assert item.context == "can run for up to 15 minutes"
    assert (
        "claim 1 states it, but none of that claim's quote spans does"
        in (llm.requests[1]["messages"][0]["content"])
    )
    assert found.verdict(85, 90).fully_supported  # a unit conversion, scored at 95
    low = assessed(ScriptedLLM([table(timeout), skeptic({1: 60})]), answer, quote)
    v = low.verdict(70, 90)
    assert not v.fully_supported and not v.core_supported  # tied to a core claim
    peripheral = claim("can run for up to 15 minutes", (1, quote), role="peripheral")
    side = assessed(ScriptedLLM([table(peripheral), skeptic({1: 60})]), answer, quote)
    assert side.verdict(70, 90).core_supported and not side.verdict(70, 90).fully_supported


def built(search: Search | None = None, search_span_missing: bool = False) -> Assessment:
    """A core claim scored 90 and a peripheral one scored 60, both quoted, and a skeptic reply."""
    claims = [
        CheckedClaim(
            answer_span=SPAN,
            role="core",
            support=[CheckedSupport(quote=1, quote_span=SPAN)],
            score=90,
            why="quote 1 states it",
        ),
        CheckedClaim(
            answer_span="Bucket names",
            role="peripheral",
            support=[CheckedSupport(quote=1, quote_span="Bucket names must")],
            score=60,
            why="a reading of the quote",
        ),
    ]
    none = Search(found=False, answer_span="", role="peripheral", why="", confidence=0)
    reply = SkepticReply(flagged=[], search=search or none)
    return Assessment(claims=claims, skeptic=reply, search_span_missing=search_span_missing)


def test_the_verdict_follows_t_s_and_the_skeptics_search() -> None:
    found = built()
    assert found.verdict(50, None).fully_supported and found.verdict(50, None).support_score == 0.6
    at_70 = found.verdict(70, None)
    assert not at_70.fully_supported and at_70.core_supported  # the peripheral claim, at 60
    assert at_70.reason == '"Bucket names" (peripheral), score 60: a reading of the quote'
    core_miss = built(Search(found=True, answer_span=SPAN, role="core", why="no", confidence=80))
    assert core_miss.verdict(50, None).fully_supported  # s off: the search fails nothing
    assert core_miss.verdict(50, 90).fully_supported  # 80 is under s
    at_s70 = core_miss.verdict(50, 70)
    assert not at_s70.fully_supported and not at_s70.core_supported
    assert at_s70.reason.startswith(f'"{SPAN}" (core), the skeptic: no')
    side = built(Search(found=True, answer_span=SPAN, role="peripheral", why="no", confidence=80))
    assert not side.verdict(50, 70).fully_supported and side.verdict(50, 70).core_supported
    missing = built(
        Search(found=True, answer_span="words", role="core", why="no", confidence=99),
        search_span_missing=True,
    )
    assert missing.verdict(50, 70).fully_supported  # read as none found
    for case in (found, core_miss, side, missing):
        for t in (50, 70, 85):
            for s in (None, 70, 90):
                v = case.verdict(t, s)
                assert v.core_supported or not v.fully_supported  # fully implies core


def test_a_verdict_fails_only_because_of_a_dropped_entry_when_putting_it_back_passes_it() -> None:
    held = CheckedSupport(quote=1, quote_span=SPAN)
    lost = CheckedSupport(quote=1, quote_span=SPAN, problem="not in quote 1")
    none = Search(found=False, answer_span="", role="core", why="", confidence=0)

    def found(
        *claims: tuple[int, list[CheckedSupport]], items: dict[int, int] | None = None
    ) -> Assessment:
        """One core claim per (score, support entries), and the skeptic's scores of flagged
        items, each a number of the first claim that no quote span of it holds."""
        checked = [
            CheckedClaim(answer_span=SPAN, role="core", support=support, score=score, why="q")
            for score, support in claims
        ]
        flagged = [
            cj.FlaggedItem(number=n, kind="number_not_quoted", token="63", context=SPAN, claim=1)
            for n in items or {}
        ]
        scores = [
            cj.ItemScore(item=n, score=score, not_a_claim=False, why="w")
            for n, score in (items or {}).items()
        ]
        reply = SkepticReply(flagged=scores, search=none)
        dropped = sum(e.problem is not None for _, support in claims for e in support)
        return Assessment(claims=checked, flagged=flagged, skeptic=reply, dropped_support=dropped)

    flips = found((80, [lost]))  # with its one entry back, it passes at t = 50 and 70
    assert [flips.dropped_span_flip(t, None) for t in cj.T_GRID] == [True, True, False]
    assert not flips.verdict(50, None).fully_supported  # a flip is still a fail
    assert not found((80, [held])).dropped_span_flip(70, 90)  # it passes: nothing flips
    passes = found((80, [held, lost]))  # one entry dropped, but one holds: it passes
    assert passes.verdict(70, 90).fully_supported and not passes.dropped_span_flip(70, 90)
    assert not found((30, [lost])).dropped_span_flip(50, None)  # it fails with its entry back too
    assert not found((30, [held])).dropped_span_flip(50, None)  # it fails, and nothing was dropped
    assert not found((90, [lost]), (30, [held])).dropped_span_flip(70, None)  # another one fails
    # The rest stays as it was, flagged items included: a low item still fails it.
    assert not found((90, [lost]), items={1: 40}).dropped_span_flip(70, None)
    assert found((90, [lost]), items={1: 95}).dropped_span_flip(70, None)
    assert not Assessment(error="the claim table failed twice").dropped_span_flip(70, 90)


def test_the_grader_counts_what_its_checks_found_in_each_answer_it_judged() -> None:
    lost = claim(SPAN, (9, SPAN), (8, SPAN))  # no quote 9 or 8: the claim keeps no entry
    made_up = skeptic(span="Bucket names are global", role="core", confidence=95)
    llm = ScriptedLLM(
        [
            *(table(QUOTED), skeptic()),  # every claim quoted
            *(table(lost), skeptic({1: 95, 2: 95})),  # with its entries back it passes at 70, 90
            *([text("no table")], [text("none again")]),  # a judge error after a retry
            # Both calls asked again, and the search names words not in the answer twice.
            *([text("no table")], table(QUOTED), made_up, made_up),
        ]
    )
    judge = grader(llm)
    assert judge.counts == cj.JudgingCounts()
    for _ in range(4):
        judge.grade(make_case(), answered())
    uncited = AgentResult(output={"answer": "63", "citations": [], "abstained": False})
    assert judge.grade(make_case(), uncited) == []  # nothing to judge: not a judging
    assert judge.counts == cj.JudgingCounts(
        judgings=4,
        retried=2,  # judgings with a reply asked again, not the replies asked again (3)
        judge_errors=1,
        support_entries=4,
        dropped=2,
        with_dropped=1,
        search_missing=1,
        dropped_span_flips=1,  # at the grader's own t = 70 and s = 90
    )
    assert judge.run_note()["counts"] == judge.counts.model_dump()
    strict = grader(ScriptedLLM([table(lost), skeptic({1: 80, 2: 80})]), support_threshold=85)
    strict.grade(make_case(), answered())  # the items' 80 fail it at t = 85, entries or not
    assert (strict.counts.dropped, strict.counts.dropped_span_flips) == (2, 0)


class Replies(ScriptedLLM):
    """The claim judge's replies by call and case, from any thread: each salt's next one."""

    def __init__(self, replies: dict[str, list[list[dict[str, Any]]]]) -> None:
        super().__init__([])
        self.replies = replies  # "call#case id" -> its replies, in order
        self.lock = threading.Lock()

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        _, call, case_id = salt.split("#")
        with self.lock:
            self.script = [self.replies[f"{call}#{case_id}"].pop(0)]
            return super().create(request, salt=salt)


def test_answers_judged_in_parallel_are_each_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    add = cj.JudgingCounts.add

    def slow_add(self: cj.JudgingCounts, other: cj.JudgingCounts) -> cj.JudgingCounts:
        time.sleep(0.002)  # a wide gap between reading the counts and writing them back
        return add(self, other)

    monkeypatch.setattr(cj.JudgingCounts, "add", slow_add)
    cases = [make_case(id=f"t-{n}") for n in range(40)]
    replies = {f"claims#{c.id}": [table(QUOTED)] for c in cases}
    replies |= {f"skeptic#{c.id}": [skeptic()] for c in cases}
    judge = grader(Replies(replies))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda case: judge.grade(case, answered()), cases))
    assert (judge.counts.judgings, judge.counts.support_entries) == (40, 40)
    assert judge.usage.model_calls == 80


def test_a_flagged_item_without_a_skeptic_score_fails_and_an_error_has_no_verdict() -> None:
    found = built()
    found.flagged = [cj.FlaggedItem(number=1, kind="uncovered", token="Region", context="x")]
    found.skeptic = None
    v = found.verdict(50, None)
    assert not v.fully_supported and v.core_supported and "no skeptic score" in v.reason
    with pytest.raises(ValueError, match="no verdict"):
        Assessment(error="the claim table failed twice").verdict(70, 90)


def test_a_claim_an_item_and_the_search_count_at_exactly_their_threshold() -> None:
    at_70 = claim(SPAN, (1, SPAN), score=70)
    search = skeptic(span="Bucket names must be", role="core", confidence=70)
    found = assessed(ScriptedLLM([table(at_70), search]))
    assert found.verdict(70, None).fully_supported and not found.verdict(85, None).fully_supported
    assert not found.verdict(50, 70).fully_supported and found.verdict(50, 90).fully_supported
    items = skeptic({1: 70, 2: 70, 3: 70, 4: 70})
    flagged = assessed(ScriptedLLM([table(QUOTED, not_claims=(NO_CLAIM,)), items]), FLAGGED_ANSWER)
    assert flagged.verdict(70, None).fully_supported
    assert not flagged.verdict(85, None).fully_supported


def test_no_claim_clears_a_name_but_a_number_only_on_its_support_score() -> None:
    items = [
        cj.FlaggedItem(number=1, kind="not_claims", token="EC2", context="check the EC2 console"),
        cj.FlaggedItem(number=2, kind="not_claims", token="1,000", context="ask for 1,000"),
    ]
    none = Search(found=False, answer_span="", role="peripheral", why="", confidence=0)

    def scored(number: int) -> Assessment:
        flagged = [
            cj.ItemScore(item=1, score=0, not_a_claim=True, why="advice"),
            cj.ItemScore(item=2, score=number, not_a_claim=True, why="advice"),
        ]
        return Assessment(flagged=items, skeptic=SkepticReply(flagged=flagged, search=none))

    low = scored(69).verdict(70, None)
    assert not low.fully_supported and low.reason.startswith('"1,000" in words of no claim')
    assert scored(70).verdict(70, None).fully_supported  # its support score, at t
    assert not scored(70).verdict(85, None).fully_supported


def test_the_skeptic_scores_support_only_and_no_claim_clears_names_alone() -> None:
    judge = grader()
    prompt = " ".join(judge.skeptic_prompt.split())
    assert (
        "The score measures support only, so an item that no quote supports scores low even "
        "when those words make no claim." in prompt
    )
    assert "That clears a name, but never a number: a number passes only on its score." in prompt
    assert "score high" not in prompt  # words that state no fact no longer score high
    item = judge.skeptic_tool["input_schema"]["properties"]["flagged"]["items"]
    assert "never a number" in item["properties"]["not_a_claim"]["description"]


def test_the_skeptic_is_asked_at_the_grids_lowest_t_whatever_t_is_configured() -> None:
    sixty = claim(SPAN, (1, SPAN), score=60)
    search = skeptic(span="Bucket names must be", role="core", confidence=80)
    llm = ScriptedLLM([table(sixty), search])
    found = assessed(llm, support_threshold=85)
    assert found.flagged == [] and llm.salts[-1] == "judge#skeptic#t-1"  # it passes at t = 50
    assert found.verdict(50, None).fully_supported and not found.verdict(85, None).fully_supported
    v = found.verdict(50, 70)
    assert not v.fully_supported and not v.core_supported  # the search, at confidence 80
    at_gate = ScriptedLLM([table(claim(SPAN, (1, SPAN), score=50)), skeptic()])
    assert assessed(at_gate, support_threshold=85).skeptic is not None
    below = ScriptedLLM([table(claim(SPAN, (1, SPAN), score=49))])
    assert assessed(below, support_threshold=50).skeptic is None and len(below.requests) == 1


def test_a_failed_claim_table_is_asked_again_under_its_own_salt() -> None:
    not_there = claim("between 3 and 64 characters long", (1, SPAN))
    for first in ([text("It is supported.")], table(not_there), table(claim(SPAN, score=150))):
        llm = ScriptedLLM([first, table(QUOTED), skeptic()])
        found = assessed(llm)
        assert found.error is None and found.retries == 1
        assert llm.salts == ["judge#claims#t-1", "judge#claims-retry#t-1", "judge#skeptic#t-1"]
        assert found.verdict(70, 90).fully_supported
    cut = Stops([table(QUOTED), table(QUOTED), skeptic()], ["max_tokens", "tool_use", "tool_use"])
    assert assessed(cut).retries == 1
    refused = Stops([table(QUOTED), table(QUOTED), skeptic()], ["refusal", "tool_use", "tool_use"])
    assert assessed(refused).retries == 1


def test_a_reply_that_fails_twice_is_a_judge_error_not_a_verdict() -> None:
    not_an_object = [{"type": "tool_use", "id": "t", "name": "submit_claims", "input": [1]}]
    llm = ScriptedLLM([[text("It is supported.")], not_an_object])
    judge = grader(llm)
    [error] = judge.grade(make_case(), answered())
    assert (error.metric, error.value, error.passed) == ("judge_error", 1.0, False)
    assert error.reason == (
        "the claim table failed twice: no submit_claims call (stop_reason='end_turn'); then a "
        "malformed submit_claims call"
    )
    assert len(llm.requests) == 2 and judge.usage.model_calls == 2  # no skeptic call
    cut = ScriptedLLM([table(QUOTED), table(QUOTED)], stop_reason="max_tokens")
    [error] = grader(cut).grade(make_case(), answered())
    assert error.metric == "judge_error" and "cut off at max_tokens=8192" in error.reason


def test_a_failed_skeptic_reply_is_asked_again_then_becomes_a_judge_error() -> None:
    no_claim = table(QUOTED, not_claims=(NO_CLAIM,))
    missing = skeptic({1: 80, 2: 90, 3: 5})  # no item 4
    unknown = skeptic({1: 80, 2: 90, 3: 5, 4: 5, 9: 5})
    full = skeptic({1: 80, 2: 90, 3: 5, 4: 5}, no_claim=(3, 4))
    llm = ScriptedLLM([no_claim, missing, full])
    found = assessed(llm, FLAGGED_ANSWER)
    assert found.error is None and found.retries == 1 and found.verdict(70, 90).fully_supported
    assert llm.salts[1:] == ["judge#skeptic#t-1", "judge#skeptic-retry#t-1"]
    twice = ScriptedLLM([no_claim, missing, unknown])
    [error] = grader(twice).grade(make_case(), answered(FLAGGED_ANSWER))
    assert error.metric == "judge_error" and error.reason == (
        "the skeptic's reply failed twice: missing item keys [4]; then unknown item keys [9]"
    )


def test_a_search_span_not_in_the_answer_is_retried_then_read_as_none_found() -> None:
    made_up = skeptic(span="Bucket names are global", role="core", confidence=95)
    llm = ScriptedLLM([table(QUOTED), made_up, made_up])
    found = assessed(llm)
    assert found.search_span_missing and found.retries == 1 and found.error is None
    assert llm.salts[-1] == "judge#skeptic-retry#t-1"
    assert found.verdict(70, 70).fully_supported  # counted, and read as none found
    real = skeptic(span="Bucket names must be", role="core", confidence=95)
    fixed = assessed(ScriptedLLM([table(QUOTED), made_up, real]))
    assert not fixed.search_span_missing and fixed.retries == 1
    v = fixed.verdict(70, 90)
    assert not v.fully_supported and not v.core_supported
    assert fixed.verdict(70, None).fully_supported


def test_the_skeptics_request_is_the_same_for_every_t_and_s() -> None:
    requests = []
    for t, s in ((50, None), (85, 70), (70, 90)):
        llm = ScriptedLLM(
            [table(QUOTED, not_claims=(NO_CLAIM,)), skeptic({1: 80, 2: 90, 3: 5, 4: 5})]
        )
        assessed(llm, FLAGGED_ANSWER, support_threshold=t, skeptic_threshold=s)
        requests.append(llm.requests)
    assert requests[0] == requests[1] == requests[2]


def test_usage_counts_every_call_retries_included() -> None:
    llm = ScriptedLLM([[text("no table")], table(QUOTED), skeptic()])
    judge = grader(llm)
    judge.grade(make_case(), answered())
    assert judge.usage.model_calls == 3 and judge.usage.input_tokens == 300


def test_the_version_moves_with_t_s_the_prompts_and_templates_but_not_salt_or_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = grader().version
    for changed in (
        {"support_threshold": 85},
        {"skeptic_threshold": None},
        {"skeptic_threshold": 70},
        {"rubric_version": "v4"},  # other prompts
        {"model": {"name": "claude-sonnet-5-5", "max_tokens": 4096}},
        {"strict_tools": False},  # other tool schemas
    ):
        assert grader(**changed).version != base, changed
    priced = {"name": "claude-sonnet-5-5", "max_tokens": 8192, "pricing": {"input_per_mtok": 2.0}}
    assert grader(model=priced).version == base
    assert grader(grader_name="judge-v5-shadow").version == base
    assert ClaimJudgeGrader(ScriptedLLM([]), v5(), salt="probe").version == base
    for name, value in (
        ("CLAIMS_MESSAGE", cj.CLAIMS_MESSAGE + "\n"),
        ("SKEPTIC_MESSAGE", cj.SKEPTIC_MESSAGE + "\n"),
        ("RULES_VERSION", cj.RULES_VERSION + 1),
        ("GRADING_CODE_VERSION", cj.GRADING_CODE_VERSION + 1),
    ):
        with monkeypatch.context() as patched:
            patched.setattr(cj, name, value)
            assert grader().version != base, name


def test_the_version_holds_the_rules_own_data(monkeypatch: pytest.MonkeyPatch) -> None:
    base = grader().version
    for name, value in (
        ("_TYPOGRAPHY", cj._TYPOGRAPHY | {ord("~"): "-"}),
        ("_LIST_NUMBER", re.compile(r"\(?\d{1,2}[.)]")),
        ("_URL", re.compile(cj._URL.pattern.replace("https?", "https"))),
        ("_ABBREVIATIONS", (*cj._ABBREVIATIONS, "cf.")),
        ("_BEFORE", r"(?<![^\W_])"),
        ("MIN_QUOTE_SPAN_WORDS", 4),
        ("SKEPTIC_GATE", 70),
    ):
        with monkeypatch.context() as patched:
            patched.setattr(cj, name, value)
            assert grader().version != base, name
    in_module = {value for value in vars(cj).values() if isinstance(value, re.Pattern)}
    assert in_module == set(cj._patterns().values())  # no pattern of the rules is left out


def test_the_claim_judge_refuses_a_t_or_s_off_the_grid() -> None:
    for t in (0, 40, 60, 90, 100):
        with pytest.raises(ValueError, match="support_threshold is"):
            grader(support_threshold=t)
    for s in (0, 50, 80, 100):
        with pytest.raises(ValueError, match="skeptic_threshold is"):
            grader(skeptic_threshold=s)
    unchecked = v5().model_copy(update={"skeptic_threshold": 80})  # as a script's copy makes one
    with pytest.raises(ValueError, match="one of null, 70, 90"):
        ClaimJudgeGrader(ScriptedLLM([]), unchecked)
    cells = {
        grader(support_threshold=t, skeptic_threshold=s).version
        for t in cj.T_GRID
        for s in cj.S_GRID
    }
    assert len(cells) == 9 and cj.SKEPTIC_GATE == min(cj.T_GRID) == 50


def test_judge_v2s_version_ignores_the_new_settings() -> None:
    llm = ScriptedLLM([])
    assert JudgeGrader(llm, JudgeConfig()).version == ONLINE_JUDGE_VERSION == "2cf75fa03e09"
    tuned = JudgeConfig(support_threshold=50, skeptic_threshold=None, responsive=False)
    assert JudgeGrader(llm, tuned).version == ONLINE_JUDGE_VERSION


def test_the_verdict_judge_can_leave_out_the_responsive_question() -> None:
    verdicts = {"core_supported": "pass", "fully_supported": "pass", "reason": "quoted"}
    llm = ScriptedLLM([[tool_use("submit_support_verdict", verdicts)]])
    grades = JudgeGrader(llm, JudgeConfig(responsive=False)).grade(make_case(), answered())
    assert [g.metric for g in grades] == ["judge_core_supported", "judge_supported"]
    assert len(llm.requests) == 1


def test_each_judge_refuses_the_others_method_and_the_claim_judge_the_name_judge() -> None:
    llm = ScriptedLLM([])
    with pytest.raises(ValueError, match="verdict judge"):
        JudgeGrader(llm, v5())
    with pytest.raises(ValueError, match="ClaimJudgeGrader is the claim judge"):
        ClaimJudgeGrader(llm, JudgeConfig())
    with pytest.raises(ValueError, match="validated judge's name"):
        ClaimJudgeGrader(llm, v5(grader_name="judge"))
    with pytest.raises(ValueError, match="rubric_version"):
        ClaimJudgeGrader(llm, v5(rubric_version="v9"))


def _paragraphs(text: str) -> list[str]:
    return [" ".join(paragraph.split()) for paragraph in text.strip().split("\n\n")]


def test_rubric_v5_is_v4_plus_the_designs_two_paragraphs_word_for_word() -> None:
    v4, v5_text = SUPPORT_RUBRICS["v4"], SUPPORT_RUBRICS["v5"]
    assert v5_text.startswith(v4 + "\n\n")
    design = (ROOT / "docs" / "plans" / "judge-v5.md").read_text(encoding="utf-8")
    section = design.split("\n## The rubric (decision 2)\n", 1)[1].split("\n## ", 1)[0]
    quoted = "\n".join(line[1:].strip() for line in section.splitlines() if line.startswith(">"))
    assert len(_paragraphs(quoted)) == 2  # the blockquote, as the design proposes it
    assert _paragraphs(v5_text[len(v4) :]) == _paragraphs(quoted)
    prompts = grader()
    for prompt in (prompts.claims_prompt, prompts.skeptic_prompt):
        assert v5_text in prompt and "does not say something" in prompt


def test_the_judge_v5_config_builds_the_claim_judge() -> None:
    judge = load_config(ROOT / "configs" / "judge-v5.yaml").judge
    assert (judge.method, judge.grader_name, judge.rubric_version) == ("claims", "judge-v5", "v5")
    assert (judge.model.name, judge.model.max_tokens) == ("claude-sonnet-5-5", 8192)
    assert judge.model.pricing == load_config(ROOT / "configs" / "default.yaml").judge.model.pricing
    assert (judge.support_threshold, judge.skeptic_threshold, judge.responsive) == (70, 90, False)
    assert ClaimJudgeGrader(ScriptedLLM([]), judge).name == "judge-v5"


def _judge_config(tmp_path: Path) -> Path:
    """A claims config that leaves responsive at its default: the claim judge never asks it. It
    is priced, as `evals judge` requires of the claim judge, whose spend is the round's."""
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n'
        f'  cache_dir: "{(tmp_path / "cache").as_posix()}"\n'
        "judge:\n  method: claims\n  grader_name: judge-v5\n  rubric_version: v5\n"
        "  model:\n    name: claude-sonnet-5-5\n"
        "    pricing: {input_per_mtok: 2.0, output_per_mtok: 10.0}\n",
        encoding="utf-8",
    )
    return config


def _run(tmp_path: Path, run_id: str, online: bool) -> None:
    suite = tmp_path / "suite.jsonl"
    write_suite(suite, [make_case()])
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        manifest = {"suite": {"path": str(suite), "sha256": file_sha256(suite)}}
        store.start_run(
            run_id,
            agent="docs",
            agent_version="v",
            suite="smoke",
            attempts=1,
            manifest=manifest,
            online=online,
        )
        store.record_attempt(run_id, "t-1", 1, answered(), 0.0, [])
    finally:
        store.close()


def test_evals_judge_runs_the_claim_judge_with_no_responsive_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "r1", online=False)
    config = _judge_config(tmp_path)
    write_pilot()  # stage A's pilot comes first
    llm = ScriptedLLM([table(QUOTED), skeptic()])
    monkeypatch.setattr(llm_module, "AnthropicClient", lambda *args, **kwargs: llm)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    result = CliRunner().invoke(cli.app, ["judge", "r1", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert [r["tools"][0]["name"] for r in llm.requests] == ["submit_claims", "submit_skeptic"]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        grades = store.grades_for("r1", "judge-v5")
        note = store.run_info("r1")["manifest"]["judge-v5"]
    finally:
        store.close()
    assert grades == {
        ("t-1", 1): {
            "judge_core_supported": 1.0,
            "judge_supported": 1.0,
            "judge_support_score": 0.9,
        }
    }
    judge = ClaimJudgeGrader(llm, load_config(config).judge)
    assert note["version"] == judge.version and note["method"] == "claims"
    assert note["retry_salts"] == ["judge#claims-retry#<case id>", "judge#skeptic-retry#<case id>"]
    assert note["counts"] == cj.JudgingCounts(judgings=1, support_entries=1).model_dump()


def test_evals_judge_prints_and_notes_what_the_claim_judges_checks_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = tmp_path / "suite.jsonl"
    cases = [make_case(id=f"t-{n}") for n in (1, 2, 3)]
    write_suite(suite, cases)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        manifest = {"suite": {"path": str(suite), "sha256": file_sha256(suite)}}
        store.start_run(
            "r1", agent="docs", agent_version="v", suite="smoke", attempts=1, manifest=manifest
        )
        for case in cases:
            store.record_attempt("r1", case.id, 1, answered(), 0.0, [])
    finally:
        store.close()
    write_pilot()
    lost = claim(SPAN, (9, SPAN))  # quote 9 does not exist: the entry is dropped
    llm = Replies(
        {
            "claims#t-1": [table(QUOTED)],
            "skeptic#t-1": [skeptic()],
            "claims#t-2": [table(lost)],  # with its entry back it would pass at t 70 and s 90
            "skeptic#t-2": [skeptic({1: 95, 2: 95})],
            "claims#t-3": [[text("no table")]],
            "claims-retry#t-3": [[text("none again")]],  # a judge error
        }
    )
    monkeypatch.setattr(llm_module, "AnthropicClient", lambda *args, **kwargs: llm)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    result = CliRunner().invoke(cli.app, ["judge", "r1", "-c", str(_judge_config(tmp_path))])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert (
        "the claim judge's checks, of the 3 judgings: retried 1 of 3 (33%); judge errors 1 of 3 "
        "(33%); support entries dropped by the span check 1 of 2 (50%), in 1 of 3 (33%) "
        "judgings; a search naming words not in the answer in 0 of 3 (0%); verdicts that fail "
        "at t=70 s=90 only because of a dropped entry: 1 of 2 (50%). The run's note records "
        "these counts."
    ) in out
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        note = store.run_info("r1")["manifest"]["judge-v5"]
        grades = store.grades_for("r1", "judge-v5")
    finally:
        store.close()
    assert note["counts"] == {
        "judgings": 3,
        "retried": 1,
        "judge_errors": 1,
        "support_entries": 2,
        "dropped": 1,
        "with_dropped": 1,
        "search_missing": 0,
        "dropped_span_flips": 1,
    }
    assert (note["support_threshold"], note["skeptic_threshold"]) == (70, 90)  # the flips' cell
    assert grades[("t-2", 1)]["judge_supported"] == 0.0  # a flip is reported, and is still a fail
    assert grades[("t-3", 1)] == {"judge_error": 1.0}


def test_evals_judge_still_refuses_the_claim_judge_on_an_online_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "r-online", online=True)
    llm = ScriptedLLM([])
    monkeypatch.setattr(llm_module, "AnthropicClient", lambda *args, **kwargs: llm)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    result = CliRunner().invoke(cli.app, ["judge", "r-online", "-c", str(_judge_config(tmp_path))])
    assert result.exit_code == 2 and "validated judge" in result.output
    assert llm.requests == []
