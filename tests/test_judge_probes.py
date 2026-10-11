from __future__ import annotations

import pytest

from agent_evals.judge_probes import (
    PADDING,
    Variant,
    behaved,
    drop_qualifier,
    mutate_number,
    pad_answer,
    swap_citation,
    variants,
)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Deprecated on Oct 31, 2026.", "Deprecated on Oct 62, 2026."),  # first number doubles
        ("Up to 5 layers.", "Up to 6 layers."),
        ("900 seconds (15 minutes).", "1800 seconds (15 minutes)."),
        ("10,240 MB of memory", "20,480 MB of memory"),
        ("as little as 0.5 ACUs", "as little as 1 ACUs"),
        ("S3 bucket names are lowercase.", None),
    ],
)
def test_mutate_number(answer: str, expected: str | None) -> None:
    assert mutate_number(answer) == expected


def test_drop_qualifier_removes_the_first_bound() -> None:
    dropped = drop_qualifier("You get 1,000 by default, up to 10,000.")
    assert dropped == "You get 1,000, up to 10,000."
    assert drop_qualifier("Only lowercase letters.") is None  # sentence-initial: nothing to drop
    assert drop_qualifier("Names can be up to 63 characters") == "Names can be 63 characters"
    assert drop_qualifier("No qualifier here.") is None


def test_swap_citation_uses_an_unrelated_quote() -> None:
    cits = [{"url": "u1", "quote": "between 3 and 63"}, {"url": "u2", "quote": "lowercase"}]
    pool = [("u1", "between 3 and 63"), ("u9", "900 seconds (15 minutes)")]
    swapped = swap_citation(cits, pool)
    assert swapped == [{"url": "u9", "quote": "900 seconds (15 minutes)"}, cits[1]]
    assert swap_citation([], pool) is None
    assert swap_citation(cits, [("u1", "between 3 and 63")]) is None  # nothing unrelated


def test_variants_cover_every_applicable_probe() -> None:
    output = {
        "answer": "Up to 63 characters by default.",
        "citations": [{"url": "u1", "quote": "between 3 and 63"}],
        "abstained": False,
        "confidence": 0.9,
    }
    vs = variants(output, [("u9", "other quote")])
    assert [v.probe for v in vs] == ["number", "qualifier", "citation", "padding", "resample"]
    assert vs[0].output["answer"] == "Up to 126 characters by default."
    assert vs[1].output["answer"] == "Up to 63 characters."
    assert vs[2].output["citations"][0]["quote"] == "other quote"
    assert vs[3].output["answer"] == pad_answer(output["answer"])
    assert PADDING in vs[3].output["answer"]
    assert vs[4].output == output and vs[4].salt == "probe"
    assert all(v.salt == "judge" for v in vs[:4])
    bare = variants({"answer": "Yes.", "citations": [], "abstained": False}, [])
    assert [v.probe for v in bare] == ["padding", "resample"]


def test_behaved_demands_failure_from_mutants_and_stability_from_probes() -> None:
    mutant = Variant("number", "mutant", {}, "judge")
    probe = Variant("padding", "probe", {}, "judge")
    original = {"judge_supported": 1.0, "judge_responsive": 1.0}
    assert behaved(mutant, original, {"judge_supported": 0.0, "judge_responsive": 1.0})
    assert not behaved(mutant, original, {"judge_supported": 1.0, "judge_responsive": 1.0})
    assert behaved(probe, original, {"judge_supported": 1.0, "judge_responsive": 1.0})
    assert not behaved(probe, original, {"judge_supported": 0.0, "judge_responsive": 1.0})
    assert not behaved(probe, original, {})  # the judge errored: not stable
    assert not behaved(mutant, original, {})
