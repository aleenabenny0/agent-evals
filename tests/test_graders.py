from __future__ import annotations

from agent_evals.agents.docs.corpus import Corpus
from agent_evals.graders.deterministic import AnswerGrader, CitationGrader, TrajectoryGrader
from agent_evals.types import AgentResult, Gold, Grade, Step
from conftest import BUCKETS_URL, LAMBDA_URL, make_case


def by_metric(grades: list[Grade]) -> dict[str, Grade]:
    return {g.metric: g for g in grades}


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
    g = by_metric(CitationGrader(corpus).grade(make_case(), answer("63", [cite])))
    assert g["fabricated_citation"].passed
    assert g["citation_valid_rate"].value == 1.0


def test_fabricated_quote_and_unknown_page(corpus: Corpus) -> None:
    cites = [
        {"url": LAMBDA_URL, "quote": "Function timeout is 30 minutes for all functions"},
        {"url": "https://docs.example.test/made-up.html", "quote": "anything at all here"},
        {"url": BUCKETS_URL, "quote": "63"},
    ]
    g = by_metric(CitationGrader(corpus).grade(make_case(), answer("x", cites)))
    assert not g["fabricated_citation"].passed
    assert g["citation_valid_rate"].value == 0.0
    reason = g["fabricated_citation"].reason
    assert "quote not found" in reason and "not in snapshot" in reason and "too short" in reason


def test_uncited_answer_is_flagged(corpus: Corpus) -> None:
    g = by_metric(CitationGrader(corpus).grade(make_case(), answer("63")))
    assert not g["uncited_answer"].passed


def test_abstention_without_citations_is_fine(corpus: Corpus) -> None:
    assert CitationGrader(corpus).grade(make_case(), answer("", abstained=True)) == []


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
