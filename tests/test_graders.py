from __future__ import annotations

import pytest

from agent_evals.corpus import Corpus
from agent_evals.graders.deterministic import (
    AnswerGrader,
    CitationGrader,
    FormatGrader,
    TrajectoryGrader,
)
from agent_evals.types import AgentResult, Gold, Grade, Step
from conftest import BUCKETS_URL, LAMBDA_URL, make_case


def by_metric(grades: list[Grade]) -> dict[str, Grade]:
    return {g.metric: g for g in grades}


def citation_grader(corpus: Corpus) -> CitationGrader:
    return CitationGrader(corpus, min_quote_chars=12)


def answer(
    text: str, citations: list[dict[str, str]] | None = None, abstained: bool = False
) -> AgentResult:
    return AgentResult(
        output={
            "answer": text,
            "citations": citations or [],
            "abstained": abstained,
            "confidence": 0.5,
        }
    )


# ---- AnswerGrader --------------------------------------------------------------------


def test_correct_when_every_group_matches() -> None:
    case = make_case(gold=Gold(answer="", must_include=[["50 TB", "50TB"], ["5 GB"]]))
    g = by_metric(AnswerGrader().grade(case, answer("Up to **50TB**; a PUT is 5 GB.")))
    assert g["correct"].passed


def test_incorrect_lists_missing_group() -> None:
    case = make_case(gold=Gold(answer="", must_include=[["50 TB"], ["5 GB"]]))
    g = by_metric(AnswerGrader().grade(case, answer("The max object is 5 TB, PUT is 5 GB.")))
    assert not g["correct"].passed
    assert "50 TB" in g["correct"].reason


def test_unanswerable_rewards_abstaining() -> None:
    case = make_case(type="unanswerable", gold=Gold(answer=""))
    assert by_metric(AnswerGrader().grade(case, answer("", abstained=True)))["correct"].passed
    g = by_metric(AnswerGrader().grade(case, answer("24 hours")))
    assert not g["correct"].passed
    assert g["missed_abstention"].value == 1.0


def test_abstaining_on_answerable_is_wrong() -> None:
    g = by_metric(AnswerGrader().grade(make_case(), answer("", abstained=True)))
    assert not g["correct"].passed
    assert g["wrong_abstention"].value == 1.0


def test_agent_error_is_incorrect() -> None:
    g = by_metric(AnswerGrader().grade(make_case(), AgentResult(error="max_steps")))
    assert not g["correct"].passed and "max_steps" in g["correct"].reason


# ---- CitationGrader ------------------------------------------------------------------


def test_valid_quote_survives_markdown_and_spacing(corpus: Corpus) -> None:
    cite = {"url": BUCKETS_URL, "quote": "Bucket names must be between 3 and  63 characters long."}
    g = by_metric(citation_grader(corpus).grade(make_case(), answer("63", [cite])))
    assert g["fabricated_citation"].passed
    assert g["citation_valid_rate"].value == 1.0


def test_fabricated_quote_and_unknown_page(corpus: Corpus) -> None:
    cites = [
        {"url": LAMBDA_URL, "quote": "Function timeout is 30 minutes for all functions"},
        {"url": "https://docs.example.test/made-up.html", "quote": "anything at all here"},
        {"url": BUCKETS_URL, "quote": "63"},
    ]
    g = by_metric(citation_grader(corpus).grade(make_case(), answer("x", cites)))
    assert not g["fabricated_citation"].passed
    assert g["citation_valid_rate"].value == 0.0
    reason = g["fabricated_citation"].reason
    assert "quote not found" in reason and "not in snapshot" in reason and "too short" in reason


def test_uncited_answer_is_flagged(corpus: Corpus) -> None:
    g = by_metric(citation_grader(corpus).grade(make_case(), answer("63")))
    assert not g["uncited_answer"].passed


def test_abstention_without_citations_is_fine(corpus: Corpus) -> None:
    assert citation_grader(corpus).grade(make_case(), answer("", abstained=True)) == []


# ---- FormatGrader --------------------------------------------------------------------


def test_malformed_output_flags_tool_markup() -> None:
    leaked = 'Up to 5 GB.</answer>\n<parameter name="citations">[]'
    g = by_metric(FormatGrader().grade(make_case(), answer(leaked)))
    assert not g["malformed_output"].passed
    assert g["malformed_output"].value == 1.0
    assert "</answer>" in g["malformed_output"].reason


def test_plain_answer_is_well_formed() -> None:
    plain = "Run aws ssm get-parameter --name <parameter-name>; a PUT is 5 GB, 5 GB < 50 TB."
    g = by_metric(FormatGrader().grade(make_case(), answer(plain)))
    assert g["malformed_output"].passed
    assert g["malformed_output"].value == 0.0


def test_format_grader_skips_agent_errors() -> None:
    assert FormatGrader().grade(make_case(), AgentResult(error="max_steps")) == []


# ---- TrajectoryGrader ----------------------------------------------------------------


def test_evidence_recall_counts_read_pages() -> None:
    result = AgentResult(
        trajectory=[
            Step(index=0, kind="model", name="m"),
            Step(index=1, kind="tool", name="read_page", input={"url": BUCKETS_URL, "start": 0}),
            Step(index=2, kind="tool", name="search_docs", input={"query": "q"}, is_error=True),
        ]
    )
    g = by_metric(TrajectoryGrader().grade(make_case(), result))
    assert g["evidence_recall"].value == 1.0
    assert g["model_calls"].value == 1.0
    assert g["tool_errors"].value == 1.0


def test_rejected_submissions_are_counted() -> None:
    result = AgentResult(
        trajectory=[
            Step(index=0, kind="tool", name="submit_answer", is_error=True),
            Step(index=1, kind="tool", name="read_page", is_error=True),
            Step(index=2, kind="tool", name="submit_answer"),
        ]
    )
    g = by_metric(TrajectoryGrader().grade(make_case(), result))
    assert g["rejected_submissions"].value == 1.0
    assert g["tool_errors"].value == 2.0


def test_malformed_citation_entries_are_graded_not_raised(corpus: Corpus) -> None:
    result = AgentResult(
        output={"answer": "63", "citations": ["bad", 42], "abstained": False, "confidence": 0.5}
    )
    g = by_metric(citation_grader(corpus).grade(make_case(), result))
    assert not g["fabricated_citation"].passed
    assert g["citation_valid_rate"].value == 0.0
    assert "not an object" in g["fabricated_citation"].reason
    # The trajectory grader reads the same field and must not raise either.
    t = by_metric(TrajectoryGrader().grade(make_case(), result))
    assert t["evidence_recall"].value == 0.0


def test_citations_that_are_not_a_list_count_as_none(corpus: Corpus) -> None:
    result = AgentResult(
        output={"answer": "63", "citations": "nope", "abstained": False, "confidence": 0.5}
    )
    g = by_metric(citation_grader(corpus).grade(make_case(), result))
    assert not g["uncited_answer"].passed


def test_quote_threshold_comes_from_the_caller(corpus: Corpus) -> None:
    cite = {"url": BUCKETS_URL, "quote": "3 and 63"}  # 8 chars after normalizing
    strict = by_metric(
        CitationGrader(corpus, min_quote_chars=12).grade(make_case(), answer("x", [cite]))
    )
    lenient = by_metric(
        CitationGrader(corpus, min_quote_chars=5).grade(make_case(), answer("x", [cite]))
    )
    assert not strict["fabricated_citation"].passed
    assert lenient["fabricated_citation"].passed


def test_evidence_groups_accept_any_alternative() -> None:
    case = make_case(
        gold=Gold(
            answer="",
            must_include=[["63"]],
            evidence=[[BUCKETS_URL, LAMBDA_URL], ["https://docs.example.test/other.html"]],
        )
    )
    result = AgentResult(
        trajectory=[
            Step(index=0, kind="tool", name="read_page", input={"url": LAMBDA_URL, "start": 0})
        ]
    )
    g = by_metric(TrajectoryGrader().grade(case, result))
    assert g["evidence_recall"].value == 0.5  # first group met by an alternative, second not
    assert make_case().gold.evidence == [[BUCKETS_URL]]  # a flat list wraps to one group per URL


@pytest.mark.parametrize(
    ("answer_text", "phrase", "expected"),
    [
        ("15 MB for both", "5 MB", False),
        ("5 MB as raw bytes, 15 MB from S3", "5 MB", True),
        ("up to 100 jobs", "10", False),
        ("a maximum of 10 active jobs", "10", True),
        ("250 documents per request", "25", False),
        ("the block is 10.0.0.0/16", "/16", True),
        ("about 10%.", "10%", True),
        ("between 3-63 characters", "63", True),
        ("Bucket names: 3 to 63.", "63", True),
        ("a 630-character name", "63", False),
        ("aws-marketplace:Unsubscribe only", "aws-marketplace:Subscribe", False),
    ],
)
def test_key_phrases_match_whole_words_and_numbers(
    answer_text: str, phrase: str, expected: bool
) -> None:
    case = make_case(gold=Gold(answer="", must_include=[[phrase]]))
    assert by_metric(AnswerGrader().grade(case, answer(answer_text)))["correct"].passed is expected
