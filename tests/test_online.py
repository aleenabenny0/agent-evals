"""Phase 7 part 2: online cases have no reference answer, and nothing may score one correct."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_evals import cli, online
from agent_evals.config import JudgeConfig, Pricing
from agent_evals.corpus import Corpus
from agent_evals.graders.deterministic import (
    AnswerGrader,
    CitationGrader,
    FormatGrader,
    GroundingGrader,
    TrajectoryGrader,
)
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.manifest import file_sha256
from agent_evals.harness.runner import run_suite
from agent_evals.online import ONLINE_JUDGE_VERSION, online_case, online_suite_name
from agent_evals.report import summarize
from agent_evals.simulate import TrafficQuestion
from agent_evals.store.db import ResultsStore
from agent_evals.suites import SuiteError, check_against_corpus, load_suite, write_suite
from agent_evals.types import AgentResult, Case, Gold, RunContext, Step
from conftest import LAMBDA_URL, ScriptedLLM, make_case, tool_use, write_pilot

PREMISE = "Functions can use between 128 MB and 10,240 MB of memory."
V21_JUDGE = "judge:\n  rubric_version: v2.1\n  grader_name: judge-v2.1\n"
CLAIM_JUDGE = (  # the claim judge under its own name, its model and thresholds at the defaults
    "judge:\n  method: claims\n  grader_name: judge-v5\n  rubric_version: v5\n  responsive: false\n"
)


def _claim_judge_version() -> str:
    """The version of the claim judge CLAIM_JUDGE configures."""
    import yaml

    from agent_evals.graders.claim_judge import ClaimJudgeGrader

    config = JudgeConfig.model_validate(yaml.safe_load(CLAIM_JUDGE)["judge"])
    return ClaimJudgeGrader(ScriptedLLM([]), config).version


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


def _day() -> list[TrafficQuestion]:
    return [
        _q("lookup", "lookup", section="Memory\n\n" + PREMISE),
        _q("false_premise", "false_premise", contradicts=PREMISE),
        _q("outside_snapshot", "unanswerable", source_url=None, service="Braket", area=None),
    ]


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


DECLINED = AgentResult(output={"answer": "", "citations": [], "abstained": True})


def test_only_online_cases_go_without_gold_and_their_gold_is_a_minimal_key() -> None:
    assert _online().gold is None and not _online().is_draft  # simulated is not a draft
    key = _online(type="false_premise", gold=Gold(answer="", support=[PREMISE]))
    assert key.gold is not None and key.gold.support == [PREMISE]
    with pytest.raises(ValidationError, match="empty gold"):
        _online(gold=Gold(answer=""))
    with pytest.raises(ValidationError, match="empty gold"):
        _online(type="false_premise", gold=Gold(answer="", support=["   "]))
    with pytest.raises(ValidationError, match="only an online false premise"):
        _online(gold=Gold(answer="", support=[PREMISE]))  # a lookup has no key
    with pytest.raises(ValidationError, match="only `support`"):
        _online(gold=Gold(answer="10,240 MB", must_include=[["10,240"]]))
    with pytest.raises(ValidationError, match="only `support`"):
        _online(
            type="false_premise", gold=Gold(answer="", support=[PREMISE], evidence=[LAMBDA_URL])
        )
    with pytest.raises(ValidationError, match="absent only on an online case"):
        Case.model_validate(make_case().model_dump(exclude={"gold"}))
    for mark in ({"split": "dev"}, {"provenance": "hand"}, {"suite": "robustness"}):
        with pytest.raises(ValidationError, match="no other case"):
            _online(**mark)  # the three marks go together
    with pytest.raises(ValidationError, match="no other case"):
        make_case(provenance="simulated")


def test_a_day_becomes_online_cases_that_round_trip_and_validate(
    tmp_path: Path, corpus: Corpus
) -> None:
    lookup, premise, outside = (online_case(q) for q in _day())
    assert lookup.gold is None and lookup.area == "compute" and lookup.is_online
    assert premise.gold is not None and premise.gold.support == [PREMISE]
    assert outside.type == "unanswerable" and outside.gold is None
    assert "intent: false_premise" in (premise.notes or "")
    path = tmp_path / f"{online_suite_name(1)}.jsonl"
    write_suite(path, [lookup, premise, outside])
    assert load_suite(path) == [lookup, premise, outside]  # no must_include error online
    errors, _ = check_against_corpus([lookup, premise, outside], corpus)
    assert errors == []  # a false premise's key is not checked as reference support
    assert online_suite_name(1) == "online-day-01"


def test_a_run_or_a_file_never_mixes_online_and_offline_cases(tmp_path: Path) -> None:
    mixed = [make_case(), online_case(_day()[0])]
    path = tmp_path / "mixed.jsonl"
    write_suite(path, mixed)
    with pytest.raises(SuiteError, match="mixes online and offline"):
        load_suite(path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        with pytest.raises(ValueError, match="mixes online and offline"):
            run_suite(
                agent=_Answers(),
                cases=mixed,
                graders=[AnswerGrader()],
                store=store,
                suite_name="mixed",
                attempts=1,
                workers=1,
                pricing=Pricing(),
                budget_usd=None,
                manifest={},
                show_progress=False,
            )
    finally:
        store.close()


def test_the_keyword_grader_records_only_abstention_on_an_online_case() -> None:
    grader = AnswerGrader()
    premise = online_case(_day()[1])  # the one online case with a gold
    for case in (_online(), premise, _online(type="unanswerable")):
        assert [(g.metric, g.value) for g in grader.grade(case, _answered())] == [
            ("abstained", 0.0)
        ]
        assert [(g.metric, g.value) for g in grader.grade(case, DECLINED)] == [("abstained", 1.0)]
    assert grader.grade(_online(), AgentResult(error="boom")) == []  # errors count elsewhere
    offline = {g.metric for g in grader.grade(make_case(), DECLINED)}
    assert offline == {"abstained", "wrong_abstention", "correct"}  # offline rules unchanged


def test_evidence_metrics_are_skipped_and_reference_free_checks_still_run(corpus: Corpus) -> None:
    metrics = {g.metric for g in TrajectoryGrader().grade(_online(), _answered())}
    assert "evidence_recall" not in metrics and "evidence_precision" not in metrics
    assert "model_calls" in metrics
    citations = CitationGrader(corpus, min_quote_chars=12).grade(_online(), _answered())
    assert {g.metric for g in citations} >= {"citation_valid_rate", "fabricated_citation"}
    uncited = CitationGrader(corpus, min_quote_chars=12).grade(_online(), _answered(cite=False))
    assert [(g.metric, g.value) for g in uncited] == [("uncited_answer", 1.0)]
    assert {g.metric for g in FormatGrader().grade(_online(), _answered())} == {"malformed_output"}
    grounding = GroundingGrader(corpus).grade(_online(), _answered())
    assert [g.metric for g in grounding] == ["unsupported_entity"]


def test_the_judge_skips_responsive_online_but_still_judges_support() -> None:
    support = tool_use(
        "submit_support_verdict",
        {"core_supported": "pass", "fully_supported": "pass", "reason": "quoted"},
    )
    llm = ScriptedLLM([[support]])
    grades = JudgeGrader(llm, JudgeConfig()).grade(online_case(_day()[1]), _answered())
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
        return DECLINED if case.type == "unanswerable" else _answered()


def _run(store: ResultsStore, cases: list[Case], name: str) -> str:
    return run_suite(
        agent=_Answers(),
        cases=cases,
        graders=[AnswerGrader(), TrajectoryGrader()],
        store=store,
        suite_name=name,
        attempts=1,
        workers=1,
        pricing=Pricing(),
        budget_usd=None,
        manifest={},
        show_progress=False,
    )


def test_an_online_run_is_graded_without_a_key_and_its_cases_not_its_name_keep_it_apart(
    tmp_path: Path,
) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        suite_run = _run(store, [make_case()], "smoke")
        online_run = _run(store, [online_case(q) for q in _day()], "day-01")  # any file name
        metrics = summarize(store, online_run).metrics
        assert metrics["abstained"].mean == pytest.approx(1 / 3)
        for never in ("correct", "wrong_abstention", "missed_abstention", "evidence_recall"):
            assert never not in metrics
        assert store.run_info(online_run)["online"] and not store.run_info(suite_run)["online"]
        assert store.latest_run_id() == suite_run  # the online run is newer, but never latest
        named = _run(store, [make_case()], "online-regressions")  # a suite run with that name
        assert store.latest_run_id() == named
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


def _start(
    tmp_path: Path, run_id: str, online: bool, manifest: dict[str, Any] | None = None
) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.start_run(
            run_id,
            agent="docs",
            agent_version="v",
            suite="day-01",
            attempts=1,
            manifest=manifest or {},
            online=online,
        )
    finally:
        store.close()


def test_an_online_run_is_judged_only_by_the_pinned_judge(tmp_path: Path) -> None:
    _start(tmp_path, "r-online", online=True)
    _start(tmp_path, "r-suite", online=False)
    config = _config(tmp_path, V21_JUDGE)
    refused = CliRunner().invoke(cli.app, ["judge", "r-online", "-c", str(config)])
    assert refused.exit_code == 2
    assert "validated judge" in refused.output and ONLINE_JUDGE_VERSION in refused.output
    suite = CliRunner().invoke(cli.app, ["judge", "r-suite", "-c", str(config)])
    assert "validated judge" not in suite.output  # a candidate judge may grade a suite run


def test_each_judge_name_has_its_own_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dict(online.ONLINE_JUDGE_PINS) == {"judge": "2cf75fa03e09"}  # v2 alone, today
    assert online.judge_pin("judge") == ONLINE_JUDGE_VERSION == "2cf75fa03e09"
    assert online.pin_refusal("judge", ONLINE_JUDGE_VERSION) is None
    other = online.pin_refusal("judge", "0123456789ab")
    assert other is not None and "validated judge 2cf75fa03e09" in other
    # a pin belongs to a name: the validated judge's version under another name has none
    unpinned = online.pin_refusal("judge-v5", ONLINE_JUDGE_VERSION)
    assert unpinned is not None and "judge-v5, has no pin" in unpinned
    assert online.judge_pin("judge-v5") is None
    monkeypatch.setattr(
        online, "ONLINE_JUDGE_PINS", {"judge": ONLINE_JUDGE_VERSION, "judge-v5": "abcdefabcdef"}
    )
    assert online.pin_refusal("judge-v5", "abcdefabcdef") is None
    moved = online.pin_refusal("judge-v5", ONLINE_JUDGE_VERSION)
    assert moved is not None and "only at its pinned version abcdefabcdef" in moved
    assert online.pin_refusal("judge", ONLINE_JUDGE_VERSION) is None  # v2's pin unchanged


def test_a_pinned_claim_judge_may_grade_an_online_run_and_an_unpinned_one_may_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = {"suite": {"path": str(tmp_path / "gone.jsonl")}}  # so a judge that gets past the
    _start(tmp_path, "r-online", online=True, manifest=gone)  # pin stops before any call
    _start(tmp_path, "r-suite", online=False, manifest=gone)
    config = _config(tmp_path, CLAIM_JUDGE)

    def judge(run_id: str) -> str:
        result = CliRunner().invoke(cli.app, ["judge", run_id, "-c", str(config)])
        return " ".join(result.output.split()) + f" (exit {result.exit_code})"

    def at_the_pin(output: str) -> bool:
        return any(why in output for why in ("has no pin", "pinned version", "validated judge"))

    refused = judge("r-online")
    assert "This config's judge, judge-v5, has no pin" in refused and "(exit 2)" in refused
    assert "suite file" not in refused  # refused at the pin, before anything else
    suite = judge("r-suite")  # a candidate judge may grade a suite run
    assert not at_the_pin(suite) and "suite file" in suite
    stale_pin = {"judge": ONLINE_JUDGE_VERSION, "judge-v5": "0123456789ab"}
    monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", stale_pin)
    stale = judge("r-online")
    assert "judge-v5 only at its pinned version 0123456789ab" in stale and "(exit 2)" in stale
    pinned = {"judge": ONLINE_JUDGE_VERSION, "judge-v5": _claim_judge_version()}
    monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", pinned)
    allowed = judge("r-online")  # past the pin; it stops only because the run has no cases
    assert not at_the_pin(allowed) and "suite file" in allowed
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # nothing was judged or noted on the way
        assert "judge-v5" not in store.run_info("r-online")["manifest"]
    finally:
        store.close()


def test_online_run_judges_with_a_pinned_claim_judge_and_refuses_an_unpinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _traffic(tmp_path, monkeypatch)
    calls: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> str:
        calls["run"] = kwargs
        _start(tmp_path, "r-1", online=True)
        return "r-1"

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "judge", lambda **kwargs: calls.setdefault("judge", kwargs))
    config = _config(tmp_path, CLAIM_JUDGE)
    result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(config)])
    out = " ".join(result.output.split())
    assert result.exit_code == 2 and "judge-v5, has no pin" in out and "Nothing was run" in out
    assert not calls  # refused before the agent was paid for
    pinned = {"judge": ONLINE_JUDGE_VERSION, "judge-v5": _claim_judge_version()}
    monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", pinned)
    # Its spend is the judge round's, which waits for stage A's pilot: `evals judge` would refuse
    # it after the agent has run, so it is refused before.
    for pilot, why in ((None, "the pilot comes first"), (True, "the pilot recorded a stop")):
        if pilot is not None:
            write_pilot(0.05, stop=pilot)
        result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(config)])
        out = " ".join(result.output.split())
        assert result.exit_code == 2 and f"judge-v5, cannot judge yet: {why}" in out, out
        assert "Nothing was run" in out and not calls
    write_pilot(0.02, stop=False)
    result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert calls["run"]["config"] == config and calls["judge"]["config"] == config
    broken = _config(tmp_path, CLAIM_JUDGE + "  support_threshold: 60\n")  # off stage A's grid
    result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(broken)])
    out = " ".join(result.output.split())
    assert result.exit_code == 2 and "cannot be built" in out and "Nothing was run" in out


def test_an_online_run_whose_cases_file_changed_is_refused(tmp_path: Path) -> None:
    cases = tmp_path / "online-day-01.jsonl"
    write_suite(cases, [online_case(q) for q in _day()])
    manifest = {"suite": {"path": str(cases), "sha256": file_sha256(cases)}}
    _start(tmp_path, "r-online", online=True, manifest=manifest)
    write_suite(cases, [online_case(q) for q in _day()[:2]])  # the day regenerated since
    result = CliRunner().invoke(cli.app, ["judge", "r-online", "-c", str(_config(tmp_path))])
    assert result.exit_code == 2 and "has changed since online run" in " ".join(
        result.output.split()
    )


def test_the_gate_refuses_online_runs(tmp_path: Path) -> None:
    _start(tmp_path, "a", online=True)
    _start(tmp_path, "b", online=True)
    result = CliRunner().invoke(cli.app, ["gate", "a", "b", "-c", str(_config(tmp_path))])
    assert result.exit_code == 2 and "monitor" in result.output


def _traffic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    traffic = tmp_path / "traffic"
    traffic.mkdir()
    (traffic / "day-01.jsonl").write_text(
        "".join(q.model_dump_json() + "\n" for q in _day()), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "TRAFFIC_DIR", traffic)
    monkeypatch.setattr(online, "ONLINE_CASES_DIR", tmp_path / "cases")
    return traffic


def test_online_run_refuses_a_wrong_judge_before_paying_for_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _traffic(tmp_path, monkeypatch)

    def never(**_: Any) -> str:
        raise AssertionError("the agent ran before the judge was checked")

    monkeypatch.setattr(cli, "run", never)
    config = _config(tmp_path, V21_JUDGE)
    result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(config)])
    assert result.exit_code == 2 and "Nothing was run" in " ".join(result.output.split())


def test_online_run_writes_the_day_runs_once_judges_and_records_its_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traffic = _traffic(tmp_path, monkeypatch)
    calls: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> str:
        calls["run"] = kwargs
        _start(tmp_path, "r-1", online=True)
        return "r-1"

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "judge", lambda **kwargs: calls.setdefault("judge", kwargs))
    config = _config(tmp_path)
    result = CliRunner().invoke(cli.app, ["online-run", "1", "-c", str(config)])
    assert result.exit_code == 0, result.output
    cases_path = tmp_path / "cases" / "online-day-01.jsonl"
    assert [c.is_online for c in load_suite(cases_path)] == [True, True, True]
    assert calls["run"]["suite"] == cases_path and calls["run"]["attempts"] == 1
    assert calls["run"]["agent"] == "docs" and calls["judge"]["run_id"] == "r-1"
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        role = store.run_info("r-1")["manifest"]["online"]
    finally:
        store.close()
    assert role == {
        "day": 1,
        "config": config.as_posix(),
        "champion": False,
        "shadow": False,
        "traffic_sha256": file_sha256(traffic / "day-01.jsonl"),  # what the monitor checks
    }

    def fake_shadow(**kwargs: Any) -> str:  # the champion on the same day, as part 5's shadow
        _start(tmp_path, "r-2", online=True)
        return "r-2"

    monkeypatch.setattr(cli, "run", fake_shadow)
    shadow = CliRunner().invoke(
        cli.app, ["online-run", "1", "--shadow", "--no-judge", "-c", str(config)]
    )
    assert shadow.exit_code == 0, shadow.output
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        role = store.run_info("r-2")["manifest"]["online"]
    finally:
        store.close()
    assert role["shadow"] is True and role["day"] == 1  # what keeps it out of `monitor 1`


def test_the_label_screen_shows_no_reference_for_an_online_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._show_reference(online_case(_day()[1]))
    shown = " ".join(capsys.readouterr().out.split())
    assert "no reference answer" in shown and "reference answer (gold)" not in shown
    assert PREMISE in shown  # a false premise shows the sentence it contradicts
    cli._show_reference(make_case())
    assert "reference answer (gold)" in capsys.readouterr().out
