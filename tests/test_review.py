from __future__ import annotations

from agent_evals.review import accept, quote_context, reject
from conftest import make_case

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
