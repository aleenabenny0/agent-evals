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


def support(core: str, full: str, reason: str = "because") -> list[dict[str, object]]:
    return [
        tool_use(
            "submit_support_verdict",
            {"core_supported": core, "fully_supported": full, "reason": reason},
        )
    ]


def answered(answer: str, cite: bool = True) -> AgentResult:
    cits = [{"url": BUCKETS_URL, "quote": "between 3 and 63 characters long"}] if cite else []
    return AgentResult(
        output={"answer": answer, "citations": cits, "abstained": False, "confidence": 0.8}
    )


def test_three_verdicts_and_what_each_prompt_may_see() -> None:
    llm = ScriptedLLM(
        [support("pass", "fail", "the 10 GB claim is not quoted"), verdict("fail", "different q")]
    )
    case = make_case(gold=Gold(answer="3 to 63 characters", must_include=[["63"]]))
    grades = JudgeGrader(llm, JudgeConfig()).grade(case, answered("Up to 63 characters."))
    by = {g.metric: g for g in grades}
    assert by["judge_core_supported"].passed and by["judge_core_supported"].value == 1.0
    assert not by["judge_supported"].passed and "10 GB" in by["judge_supported"].reason
    assert not by["judge_responsive"].passed and "different q" in by["judge_responsive"].reason
    supported_req, responsive_req = llm.requests
    # The support prompt sees the citations and never the gold answer; responsive sees the gold.
    assert "between 3 and 63" in supported_req["messages"][0]["content"]
    assert "3 to 63 characters" not in supported_req["messages"][0]["content"]
    assert "3 to 63 characters" in responsive_req["messages"][0]["content"]
    assert "tool_choice" not in supported_req  # Sonnet 5.5 rejects a forced tool (400)
    assert supported_req["tools"][0]["name"] == "submit_support_verdict"
    assert responsive_req["tools"][0]["name"] == "submit_verdict"
    assert supported_req["model"] == "claude-sonnet-5-5"
    assert "core claims" in supported_req["system"].lower()


def test_fully_supported_implies_core_supported() -> None:
    llm = ScriptedLLM([support("fail", "pass"), verdict("pass")])  # a contradictory judge
    grades = JudgeGrader(llm, JudgeConfig()).grade(make_case(), answered("63"))
    by = {g.metric: g for g in grades}
    assert by["judge_core_supported"].passed and by["judge_supported"].passed


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
    bad = ScriptedLLM([support("maybe", "pass")])
    graded = grade_all([JudgeGrader(bad, JudgeConfig())], make_case(), answered("x"))
    assert graded[0][1].metric == "grader_error" and "validation" in graded[0][1].reason
    wrong_tool = ScriptedLLM([verdict("pass")])  # the one-verdict tool where two were asked
    graded = grade_all([JudgeGrader(wrong_tool, JudgeConfig())], make_case(), answered("x"))
    assert graded[0][1].metric == "grader_error"


def test_judge_version_tracks_model_and_salt_is_separate() -> None:
    llm = ScriptedLLM([])
    a = JudgeGrader(llm, JudgeConfig())
    b = JudgeGrader(llm, JudgeConfig.model_validate({"model": {"name": "claude-opus-5-5"}}))
    c = JudgeGrader(llm, JudgeConfig(), salt="probe")
    assert a.version != b.version and a.version == c.version  # salt re-samples, same judge


def test_usage_is_counted_across_calls() -> None:
    llm = ScriptedLLM([support("pass", "pass"), verdict("pass")])
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
    # "pending": the strict question was answered but the core one never asked; "c" had no
    # citations (supported is None) so there is nothing to redo there.
    labels[0]["core_supported"] = True
    assert [x["case_id"] for x in revisit_targets(labels, "pending")] == ["b"]
    with pytest.raises(ValueError):
        revisit_targets(labels, "noted")


def test_disagreements_are_labels_the_judge_disputes_on_any_verdict() -> None:
    from agent_evals.judge_validation import disagreements

    def label(cid: str, core: bool | None, full: bool | None, resp: bool) -> dict[str, object]:
        return {
            "case_id": cid,
            "attempt": 1,
            "core_supported": core,
            "supported": full,
            "responsive": resp,
        }

    def verdicts(core: float, full: float, resp: float) -> dict[str, tuple[float, str]]:
        return {
            "judge_core_supported": (core, ""),
            "judge_supported": (full, ""),
            "judge_responsive": (resp, ""),
        }

    labels = [
        label("a", True, True, True),
        label("b", True, False, True),
        label("c", None, None, True),
    ]
    judge = {
        ("a", 1): verdicts(1.0, 0.0, 1.0),
        ("b", 1): verdicts(1.0, 0.0, 1.0),
        ("c", 1): {"judge_responsive": (0.0, "off topic")},
    }
    # a: differs on fully; b: agrees everywhere; c: differs on responsive (support never asked).
    assert [x["case_id"] for x in disagreements(labels, judge)] == ["a", "c"]
    assert disagreements(labels, {}) == []  # no verdicts yet: nothing to dispute


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


def test_candidate_judge_has_its_own_name_rubric_and_version() -> None:
    llm = ScriptedLLM([])
    validated = JudgeGrader(llm, JudgeConfig())
    candidate = JudgeGrader(
        llm,
        JudgeConfig.model_validate(
            {"grader_name": "judge-v3", "rubric_version": "v3", "model": {"temperature": 0.0}}
        ),
    )
    assert validated.name == "judge" and candidate.name == "judge-v3"
    assert "75" in candidate.supported_prompt and "75" not in validated.supported_prompt
    assert validated.version != candidate.version
    # The name alone does not change the version: same judge stored under another name.
    renamed = JudgeGrader(llm, JudgeConfig.model_validate({"grader_name": "shadow"}))
    assert renamed.version == validated.version
    with pytest.raises(ValueError, match="rubric_version"):
        JudgeGrader(llm, JudgeConfig.model_validate({"rubric_version": "v9"}))


def test_rubric_v4_adds_the_labelers_two_decisions_to_v2() -> None:
    from agent_evals.graders.judge_prompts import SUPPORT_RUBRICS

    v2, v4 = SUPPORT_RUBRICS["v2"], SUPPORT_RUBRICS["v4"]
    assert v4.startswith(v2) and "does\nnot say something" in v4 and "is a hedge" in v4
    # Billing: price factors are peripheral, the opposite of rubric v3's billing sentence.
    assert "factors that set the price" in v4 and "are peripheral" in v4
    assert "are core claims when the question asks how" not in v4
    llm = ScriptedLLM([])
    draft = JudgeGrader(
        llm, JudgeConfig.model_validate({"grader_name": "judge-v4", "rubric_version": "v4"})
    )
    assert draft.name == "judge-v4" and draft.version != JudgeGrader(llm, JudgeConfig()).version
