"""Phase 7 part 2: online cases have no reference answer, and nothing may score one correct."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.config import JudgeConfig, Pricing
from agent_evals.corpus import Corpus
from agent_evals.graders.deterministic import AnswerGrader, CitationGrader, TrajectoryGrader
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.runner import run_suite
from agent_evals.online import ONLINE_JUDGE_VERSION, online_case, online_suite_name
from agent_evals.report import summarize
from agent_evals.simulate import TrafficQuestion
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite, write_suite
from agent_evals.types import AgentResult, Case, Gold, RunContext, Step
from conftest import LAMBDA_URL, ScriptedLLM, make_case, tool_use

PREMISE = "Functions can use between 128 MB and 10,240 MB of memory."


def _q(intent: str, kind: str, **extra: Any) -> TrafficQuestion:
    fields: dict[str, Any] = {
        "id": f"online-d01-{intent}",
        "day": 1,
        "persona": "on-call",
        "intent": intent,
        "type": kind,
        "question": "How much memory can a Lambda function use?",
        "source_url": LAMBDA_URL,
        "area": "compute",
    }
    return TrafficQuestion(**(fields | extra))


def _online(**overrides: Any) -> Case:
    base: dict[str, Any] = {
        "id": "online-d01-000",
        "suite": "online",
        "agent": "docs",
        "type": "lookup",
        "question": "How much memory can a Lambda function use?",
        "split": "online",
        "provenance": "simulated",
    }
    return Case.model_validate(base | overrides)


def _answered(cite: bool = True) -> AgentResult:
    citations = [{"url": LAMBDA_URL, "quote": PREMISE}] if cite else []
    return AgentResult(
        output={"answer": "Up to 10,240 MB.", "citations": citations, "abstained": False},
        trajectory=[Step(index=0, kind="tool", name="read_page", input={"url": LAMBDA_URL})],
    )


def test_only_online_cases_go_without_gold_and_their_gold_is_a_minimal_key() -> None:
    assert _online().gold is None and not _online().is_draft  # simulated is not a draft
    key = _online(type="false_premise", gold=Gold(answer="", support=[PREMISE]))
    assert key.gold is not None and key.gold.support == [PREMISE]
    with pytest.raises(ValidationError, match="empty gold"):
        _online(gold=Gold(answer=""))
    with pytest.raises(ValidationError, match="only `support`"):
        _online(gold=Gold(answer="10,240 MB", must_include=[["10,240"]]))
    with pytest.raises(ValidationError, match="only `support`"):
        _online(gold=Gold(answer="", support=[PREMISE], evidence=[LAMBDA_URL]))
    with pytest.raises(ValidationError, match="absent only on an online case"):
        Case.model_validate(make_case().model_dump(exclude={"gold"}))
    with pytest.raises(ValidationError, match="no other case"):
        _online(split="dev")  # the three marks go together
    with pytest.raises(ValidationError, match="no other case"):
        make_case(provenance="simulated")


def test_a_day_becomes_online_cases_that_round_trip_through_a_suite_file(tmp_path: Path) -> None:
    lookup = online_case(_q("lookup", "lookup", section="Memory\n\n" + PREMISE))
    premise = online_case(_q("false_premise", "false_premise", contradicts=PREMISE))
    outside = online_case(
        _q("outside_snapshot", "unanswerable", source_url=None, service="Braket", area=None)
    )
    assert lookup.gold is None and lookup.area == "compute" and lookup.is_online
    assert premise.gold is not None and premise.gold.support == [PREMISE]
    assert outside.type == "unanswerable" and outside.gold is None
    assert "intent: false_premise" in (premise.notes or "")
    path = tmp_path / f"{online_suite_name(1)}.jsonl"
    write_suite(path, [lookup, premise, outside])
    assert load_suite(path) == [lookup, premise, outside]  # no must_include error online
    assert online_suite_name(1) == "online-day-01"


def test_the_keyword_grader_records_only_abstention_on_an_online_case() -> None:
    grader = AnswerGrader()
    answered = grader.grade(_online(), _answered())
    assert [(g.metric, g.value) for g in answered] == [("abstained", 0.0)]
    declined = AgentResult(output={"answer": "", "citations": [], "abstained": True})
    outside = _online(type="unanswerable")
    assert [(g.metric, g.value) for g in grader.grade(outside, declined)] == [("abstained", 1.0)]
    assert grader.grade(_online(), AgentResult(error="boom")) == []  # errors count elsewhere
    offline = {g.metric for g in grader.grade(make_case(), declined)}
    assert offline == {"abstained", "wrong_abstention", "correct"}  # offline rules unchanged


def test_evidence_metrics_are_skipped_and_reference_free_checks_still_run(corpus: Corpus) -> None:
    metrics = {g.metric for g in TrajectoryGrader().grade(_online(), _answered())}
    assert "evidence_recall" not in metrics and "evidence_precision" not in metrics
    assert "model_calls" in metrics
    citations = CitationGrader(corpus, min_quote_chars=12).grade(_online(), _answered())
    assert {g.metric for g in citations} >= {"citation_valid_rate", "fabricated_citation"}


def test_the_judge_skips_responsive_online_but_still_judges_support() -> None:
    support = tool_use(
        "submit_support_verdict",
        {"core_supported": "pass", "fully_supported": "pass", "reason": "quoted"},
    )
    llm = ScriptedLLM([[support]])
    premise = _online(type="false_premise", gold=Gold(answer="", support=[PREMISE]))
    grades = JudgeGrader(llm, JudgeConfig()).grade(premise, _answered())
    assert {g.metric for g in grades} == {"judge_core_supported", "judge_supported"}
    assert len(llm.requests) == 1  # no responsive call, even with a minimal key
    assert JudgeGrader(llm, JudgeConfig()).version == ONLINE_JUDGE_VERSION  # the pinned judge


class _Answers:
    """Answers every case with one cited sentence; declines outside-snapshot questions."""

    name = "fake"
    version = "v-test"

    def describe(self) -> dict[str, Any]:
        return {}

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        if case.type == "unanswerable":
            return AgentResult(output={"answer": "", "citations": [], "abstained": True})
        return _answered()


def test_an_online_run_is_graded_without_a_key_and_is_never_the_latest(
    tmp_path: Path, corpus: Corpus
) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        suite_run = run_suite(
            agent=_Answers(),
            cases=[make_case()],
            graders=[AnswerGrader()],
            store=store,
            suite_name="smoke",
            attempts=1,
            workers=1,
            pricing=Pricing(),
            budget_usd=None,
            manifest={},
            show_progress=False,
        )
        cases = [
            online_case(_q("lookup", "lookup")),
            online_case(_q("outside_snapshot", "unanswerable", source_url=None, service="Braket")),
        ]
        online_run = run_suite(
            agent=_Answers(),
            cases=cases,
            graders=[AnswerGrader(), TrajectoryGrader()],
            store=store,
            suite_name=online_suite_name(1),
            attempts=1,
            workers=1,
            pricing=Pricing(),
            budget_usd=None,
            manifest={},
            show_progress=False,
        )
        metrics = summarize(store, online_run).metrics
        assert "abstained" in metrics and metrics["abstained"].mean == 0.5
        for never in ("correct", "wrong_abstention", "missed_abstention", "evidence_recall"):
            assert never not in metrics
        assert store.latest_run_id() == suite_run  # the online run is newer, but never latest
    finally:
        store.close()


def _config(tmp_path: Path, judge: str = "") -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n'
        f'  cache_dir: "{(tmp_path / "cache").as_posix()}"\n{judge}',
        encoding="utf-8",
    )
    return config


def test_an_online_run_is_judged_only_by_the_pinned_judge(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    store.start_run(
        "r-online", agent="docs", agent_version="v", suite="online-day-01", attempts=1, manifest={}
    )
    store.close()
    config = _config(tmp_path, "judge:\n  rubric_version: v2.1\n  grader_name: judge-v2.1\n")
    result = CliRunner().invoke(cli.app, ["judge", "r-online", "-c", str(config)])
    assert result.exit_code == 2
    assert "validated judge" in result.output and ONLINE_JUDGE_VERSION in result.output


def test_online_run_needs_the_day_generated_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "TRAFFIC_DIR", tmp_path / "traffic")
    result = CliRunner().invoke(cli.app, ["online-run", "4", "-c", str(_config(tmp_path))])
    assert result.exit_code == 2 and "uv run evals simulate 4" in " ".join(result.output.split())
