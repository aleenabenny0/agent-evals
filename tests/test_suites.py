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
from conftest import BUCKETS_URL, LAMBDA_URL, make_case


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


def test_support_is_a_list_and_every_fact_needs_a_quote(corpus: Corpus) -> None:
    two_facts = make_case(
        id="two",
        area="storage",
        gold=Gold(
            answer="3 to 63 characters, lowercase",
            must_include=[["63"], ["lowercase"]],
            evidence=[BUCKETS_URL],
            support="between 3 and 63 characters long",  # shorthand for a one-item list
        ),
    )
    assert two_facts.gold.support == ["between 3 and 63 characters long"]
    _, warnings = check_against_corpus([two_facts], corpus)
    expected = "two: required fact not covered by any support quote: lowercase"
    assert expected in warnings
    covered = two_facts.model_copy(
        update={
            "gold": Gold(
                answer="3 to 63 characters, lowercase",
                must_include=[["63"], ["lowercase"]],
                evidence=[BUCKETS_URL],
                support=["between 3 and 63 characters long", "only lowercase letters"],
            )
        }
    )
    errors, warnings = check_against_corpus([covered], corpus)
    assert errors == [] and not any("not covered" in w for w in warnings)


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


def test_held_cases_never_land_in_test() -> None:
    cases = [_verified(f"c{i}", "lookup", "storage") for i in range(10)]
    cases[0] = cases[0].model_copy(update={"hold": "needs the judge"})
    cases[1] = cases[1].model_copy(update={"hold": "needs the judge"})
    for seed in range(5):
        dev, test = stratified_split(cases, dev=5, test=5, seed=seed)
        assert {c.id for c in dev} >= {"c0", "c1"} and all(c.hold is None for c in test)
        assert len(dev) == 5 and len(test) == 5
    with pytest.raises(ValueError):
        stratified_split(cases, dev=1, test=9, seed=0)  # only 8 cases are free for test


def test_validator_uses_whole_phrase_matching(corpus: Corpus) -> None:
    substring_only = make_case(
        id="sub",
        area="storage",
        gold=Gold(answer="x", must_include=[["6"]], evidence=[BUCKETS_URL]),
    )
    _, warnings = check_against_corpus([substring_only], corpus)
    assert any(w.startswith("sub: no must_include") for w in warnings)


def test_held_out_services_are_enforced(corpus: Corpus) -> None:
    from agent_evals.suites import check_held_out

    priced = make_case(id="p", type="unanswerable", gold=Gold(answer="n/a"))
    priced = priced.model_copy(update={"question": "How much does Lambda cost per request?"})
    vague = make_case(id="v", type="unanswerable", gold=Gold(answer="n/a"))
    vague = vague.model_copy(update={"question": "What is the default timeout for a function?"})
    errors, warnings = check_held_out([priced, vague], corpus, ["GameLift", "Lambda"])
    assert errors == [f"held-out service 'lambda' appears in the snapshot: {LAMBDA_URL}"]
    assert [w.split(":")[0] for w in warnings] == ["v"]
    assert check_held_out([priced], corpus, ["GameLift"]) == ([], [])


def test_numbers_in_the_gold_answer_need_a_quote_and_must_be_on_the_page(corpus: Corpus) -> None:
    case = make_case(
        id="n",
        area="storage",
        gold=Gold(
            answer="3 to 63 characters; a name may also be up to 255 bytes",
            must_include=[["63"]],
            evidence=[BUCKETS_URL],
            support=["and 63 characters long"],
        ),
    )
    errors, warnings = check_against_corpus([case], corpus)
    assert errors == []
    assert "n: number in the gold answer not on any evidence page: 255" in warnings
    assert "n: number in the gold answer not covered by any support quote: 3" in warnings
    asked = case.model_copy(update={"id": "q", "question": "Is it 3 to 63 or up to 255 bytes?"})
    _, quiet = check_against_corpus([asked], corpus)
    assert not any("number in the gold answer" in w for w in quiet)  # the question's own numbers
