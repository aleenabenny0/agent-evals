from __future__ import annotations

import pytest

from agent_evals.config import JudgeConfig
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.runner import grade_all
from agent_evals.judge_validation import agreement, label_split
from agent_evals.types import AgentResult, Gold
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use


def verdict(v: str, reason: str = "because") -> list[dict[str, object]]:
    return [tool_use("submit_verdict", {"verdict": v, "reason": reason})]


def answered(answer: str, cite: bool = True) -> AgentResult:
    cits = [{"url": BUCKETS_URL, "quote": "between 3 and 63 characters long"}] if cite else []
    return AgentResult(
        output={"answer": answer, "citations": cits, "abstained": False, "confidence": 0.8}
    )


def test_two_verdicts_and_what_each_prompt_may_see() -> None:
    llm = ScriptedLLM([verdict("pass"), verdict("fail", "answers a different question")])
    case = make_case(gold=Gold(answer="3 to 63 characters", must_include=[["63"]]))
    grades = JudgeGrader(llm, JudgeConfig()).grade(case, answered("Up to 63 characters."))
    by = {g.metric: g for g in grades}
    assert by["judge_supported"].passed and by["judge_supported"].value == 1.0
    assert not by["judge_responsive"].passed
    assert "different question" in by["judge_responsive"].reason
    supported_req, responsive_req = llm.requests
    # The supported prompt sees the citations and never the gold answer; responsive sees the gold.
    assert "between 3 and 63" in supported_req["messages"][0]["content"]
    assert "3 to 63 characters" not in supported_req["messages"][0]["content"]
    assert "3 to 63 characters" in responsive_req["messages"][0]["content"]
    assert "tool_choice" not in supported_req  # Sonnet 5.5 rejects a forced tool (400)
    assert supported_req["tools"][0]["name"] == "submit_verdict"
    assert supported_req["model"] == "claude-sonnet-5-5"


def test_judge_skips_what_code_already_settled() -> None:
    llm = ScriptedLLM([verdict("pass")])
    grader = JudgeGrader(llm, JudgeConfig())
    assert grader.grade(make_case(), AgentResult(error="max_steps")) == []
    abstained = AgentResult(output={"answer": "", "citations": [], "abstained": True})
    assert grader.grade(make_case(), abstained) == []
    # No citations: only "responsive" is asked; uncited_answer covers the rest.
    grades = grader.grade(make_case(), answered("63 characters", cite=False))
    assert [g.metric for g in grades] == ["judge_responsive"]
    assert len(llm.requests) == 1


def test_missing_or_malformed_verdict_becomes_a_grader_error() -> None:
    llm = ScriptedLLM([[text("I think it passes.")]])
    graded = grade_all([JudgeGrader(llm, JudgeConfig())], make_case(), answered("x"))
    assert [(name, g.metric) for name, g in graded] == [("judge", "grader_error")]
    assert "no verdict" in graded[0][1].reason
    bad = ScriptedLLM([[tool_use("submit_verdict", {"verdict": "maybe", "reason": "?"})]])
    graded = grade_all([JudgeGrader(bad, JudgeConfig())], make_case(), answered("x"))
    assert graded[0][1].metric == "grader_error" and "validation" in graded[0][1].reason


def test_judge_version_tracks_model_and_salt_is_separate() -> None:
    llm = ScriptedLLM([])
    a = JudgeGrader(llm, JudgeConfig())
    b = JudgeGrader(llm, JudgeConfig.model_validate({"model": {"name": "claude-opus-5-5"}}))
    c = JudgeGrader(llm, JudgeConfig(), salt="probe")
    assert a.version != b.version and a.version == c.version  # salt re-samples, same judge


def test_usage_is_counted_across_calls() -> None:
    llm = ScriptedLLM([verdict("pass"), verdict("pass")])
    grader = JudgeGrader(llm, JudgeConfig())
    grader.grade(make_case(), answered("63"))
    assert grader.usage.model_calls == 2 and grader.usage.input_tokens == 200


def test_label_split_is_deterministic_and_balanced() -> None:
    ids = [f"docs-x-{i:03d}" for i in range(400)]
    splits = [label_split(i) for i in ids]
    assert splits == [label_split(i) for i in ids]
    assert 150 < splits.count("judge-dev") < 250


def test_revisit_targets_pick_unnoted_labels_or_all() -> None:
    from agent_evals.judge_validation import revisit_targets

    labels = [
        {"case_id": "a", "attempt": 1, "supported": False, "responsive": True, "note": ""},
        {"case_id": "b", "attempt": 1, "supported": True, "responsive": False, "note": "  "},
        {"case_id": "c", "attempt": 2, "supported": None, "responsive": True, "note": "fine"},
    ]
    assert [x["case_id"] for x in revisit_targets(labels, "unnoted")] == ["a", "b"]
    assert revisit_targets(labels, "all") == labels
    with pytest.raises(ValueError):
        revisit_targets(labels, "noted")


def test_agreement_counts_failures_as_the_positive_class() -> None:
    # (human_pass, judge_pass): judge catches 2 of 3 real failures, passes 4 of 5 good answers.
    pairs = [
        (False, False),
        (False, False),
        (False, True),
        (True, True),
        (True, True),
        (True, True),
        (True, True),
        (True, False),
    ]
    a = agreement("supported", pairs)
    assert (a.tp, a.fp, a.fn, a.tn) == (2, 1, 1, 4) and a.n == 8
    assert a.tpr is not None and a.tpr.mean == 2 / 3
    assert a.tnr is not None and a.tnr.mean == 0.8
    assert 0 < a.kappa < 1
    perfect = agreement("responsive", [(True, True)] * 3 + [(False, False)] * 3)
    assert perfect.kappa == 1.0 and perfect.tpr is not None and perfect.tpr.mean == 1.0
    assert agreement("responsive", [(True, True)] * 3).tpr is None  # no human failures to catch
