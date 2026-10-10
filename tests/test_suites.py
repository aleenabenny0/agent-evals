from __future__ import annotations

from pathlib import Path

import pytest

from agent_evals.corpus import Corpus
from agent_evals.suites import (
    check_against_corpus,
    load_suite,
    stratified_split,
    unassigned,
    unverified_drafts,
    write_suite,
)
from agent_evals.types import Case, Gold
from conftest import BUCKETS_URL, make_case


def test_drafts_and_unassigned_are_identified() -> None:
    draft = make_case(id="d", provenance="synthetic", split="unassigned")
    verified = make_case(id="v", provenance="synthetic", split="dev", verified_by="aleena")
    hand = make_case(id="h")
    assert unverified_drafts([draft, verified, hand]) == ["d"]
    assert unassigned([draft, verified, hand]) == ["d"]
    assert draft.is_draft and not verified.is_draft and not hand.is_draft


def test_corpus_checks_catch_bad_evidence_and_quotes(corpus: Corpus) -> None:
    ok = make_case(
        id="ok",
        area="storage",
        gold=Gold(
            answer="3 to 63",
            must_include=[["63"]],
            evidence=[BUCKETS_URL],
            support="between 3 and 63 characters long",
        ),
    )
    bad_url = make_case(
        id="bad-url",
        area="storage",
        gold=Gold(answer="x", must_include=[["63"]], evidence=["https://docs.example.test/no"]),
    )
    bad_quote = make_case(
        id="bad-quote",
        area="storage",
        gold=Gold(
            answer="x",
            must_include=[["63"]],
            evidence=[BUCKETS_URL],
            support="bucket names may be 255 characters long",
        ),
    )
    paraphrase = make_case(  # no area, and a phrase the page never uses
        id="para", gold=Gold(answer="x", must_include=[["two hundred"]], evidence=[BUCKETS_URL])
    )
    errors, warnings = check_against_corpus([ok, bad_url, bad_quote, paraphrase], corpus)
    assert [e.split(":")[0] for e in errors] == ["bad-url", "bad-quote"]
    assert any(w.startswith("para: no must_include") for w in warnings)
    assert "para: no area" in warnings
    assert not any(w.startswith("ok:") for w in warnings)


def _verified(id_: str, type_: str, area: str) -> Case:
    extra = {"gold": Gold(answer="n/a")} if type_ == "unanswerable" else {}
    return make_case(
        id=id_,
        type=type_,
        area=area,
        provenance="synthetic",
        verified_by="aleena",
        split="unassigned",
        **extra,
    )


def test_stratified_split_keeps_the_mix_and_is_deterministic() -> None:
    cases = [_verified(f"l{i}", "lookup", "storage") for i in range(10)]
    cases += [_verified(f"u{i}", "unanswerable", "ai_ml") for i in range(5)]
    dev, test = stratified_split(cases, dev=9, test=6, seed=1)
    assert len(dev) == 9 and len(test) == 6
    assert all(c.split == "test" for c in test) and all(c.split == "dev" for c in dev)
    # 40% of each stratum lands in test: 4 of 10 lookups, 2 of 5 unanswerables.
    assert sum(c.type == "lookup" for c in test) == 4
    assert sum(c.type == "unanswerable" for c in test) == 2
    again = stratified_split(cases, dev=9, test=6, seed=1)
    assert [c.id for c in again[1]] == [c.id for c in test]
    other = stratified_split(cases, dev=9, test=6, seed=2)
    assert [c.id for c in other[1]] != [c.id for c in test]
    with pytest.raises(ValueError):
        stratified_split(cases, dev=9, test=5, seed=1)


def test_write_suite_round_trips(tmp_path: Path) -> None:
    cases = [make_case(id="a", area="storage"), make_case(id="b")]
    path = tmp_path / "s.jsonl"
    write_suite(path, cases)
    assert load_suite(path) == cases
