from __future__ import annotations

from agent_evals.review import (
    accept,
    answer_terms,
    quote_context,
    reject,
    term_spans,
    uncovered_terms,
)
from agent_evals.types import Gold
from conftest import BUCKETS_URL, make_case

PAGE = (
    "# Rules\n\nBucket names must be between 3 and\n63 characters long. They can\ncontain hyphens."
)


def test_quote_context_tolerates_line_breaks_and_case() -> None:
    found = quote_context(PAGE, "between 3 AND 63 characters long", width=10)
    assert found is not None
    before, match, after = found
    assert match == "between 3 and\n63 characters long"
    assert before.endswith("must be ") and after.startswith(". They")


def test_quote_context_returns_none_when_absent() -> None:
    assert quote_context(PAGE, "255 characters") is None
    assert quote_context(PAGE, "   ") is None


def test_accept_and_reject_record_the_reviewer() -> None:
    draft = make_case(id="d", provenance="synthetic", split="unassigned", notes="from page X")
    ok = accept(draft, "aleena")
    assert ok.verified_by == "aleena" and not ok.is_draft and ok.question == draft.question
    bad = reject(draft, "aleena", " gold answer contradicts the page ")
    assert bad.is_draft and bad.verified_by is None
    assert bad.notes == "from page X | REJECTED by aleena: gold answer contradicts the page"
    assert reject(make_case(id="e"), "aleena", "").notes == "REJECTED by aleena: no reason given"


def test_restore_undoes_a_rejection_and_keeps_earlier_notes() -> None:
    from agent_evals.review import restore

    plain = make_case(id="d", provenance="synthetic", split="unassigned")
    assert restore(reject(plain, "aleena", "oops")).notes is None
    noted = make_case(id="e", provenance="synthetic", split="unassigned", notes="from page X")
    assert restore(reject(noted, "aleena", "oops")).notes == "from page X"
    assert restore(reject(noted, "aleena", "oops")).is_draft


def test_answer_terms_are_required_phrases_then_new_numbers() -> None:
    case = make_case(
        question="Can a bucket name have 70 characters?",
        gold=Gold(
            answer="No: 3 to 63 characters, lowercase only; objects up to 10,240 MB.",
            must_include=[["63"], ["lowercase"]],
            evidence=[BUCKETS_URL],
        ),
    )
    # "70" is the question's number, not a claim; "63" is not repeated.
    assert answer_terms(case) == ["63", "lowercase", "3", "10240"]
    runtimes = make_case(
        question="Is python3.9 still supported on AL2?",
        gold=Gold(answer="No. Move python3.9 to python3.13 on AL2023.", must_include=[["AL2023"]]),
    )
    # Identifiers with digits are facts too; the question's own identifiers are not claims.
    assert answer_terms(runtimes) == ["AL2023", "python3.13"]


def test_term_spans_match_whole_numbers_and_printed_separators() -> None:
    text = "Names: 163 or 63 chars, LOWERCASE only, 10,240 MB max, 3 min"
    spans = term_spans(text, ["63", "lowercase", "10240", "3"])
    assert [text[a:b] for a, b in spans] == ["63", "LOWERCASE", "10,240", "3"]
    assert term_spans(text, []) == [] and term_spans("", ["63"]) == []


def test_uncovered_terms_lists_facts_the_quotes_do_not_state() -> None:
    case = make_case(
        gold=Gold(
            answer="3 to 63 characters, lowercase, and a 10,240 MB object limit",
            must_include=[["63"], ["lowercase"]],
            evidence=[BUCKETS_URL],
            support=["between 3 and 63 characters long"],
        ),
    )
    assert uncovered_terms(case) == ["lowercase", "10240"]
    covered = case.model_copy(
        update={
            "gold": case.gold.model_copy(
                update={"support": [*case.gold.support, "lowercase letters", "10,240 MB"]}
            )
        }
    )
    assert uncovered_terms(covered) == []
    assert uncovered_terms(make_case()) == []  # no quotes yet: nothing to compare against
