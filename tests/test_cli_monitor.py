"""`evals monitor` end to end on a synthetic store: freeze a quiet reference, catch a bad day."""

from __future__ import annotations

import json
import shutil
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals import monitor as mon
from agent_evals.harness.manifest import file_sha256
from agent_evals.online import ONLINE_JUDGE_VERSION, online_case
from agent_evals.simulate import TrafficQuestion
from agent_evals.store.db import ResultsStore
from agent_evals.suites import write_suite
from agent_evals.types import AgentResult, Grade, Usage
from conftest import LAMBDA_URL


def _questions(day: int) -> list[TrafficQuestion]:
    base = {"day": day, "persona": "on-call", "question": "How much memory?"}
    lookups = [
        TrafficQuestion(
            id=f"online-d{day:02d}-{i:03d}",
            intent="lookup",
            type="lookup",
            source_url=LAMBDA_URL,
            **base,
        )
        for i in range(16)
    ]
    outside = [
        TrafficQuestion(
            id=f"online-d{day:02d}-{i:03d}",
            intent="outside_snapshot",
            type="unanswerable",
            service="Braket",
            **base,
        )
        for i in range(16, 20)
    ]
    return lookups + outside


def _day(
    store: ResultsStore, tmp: Path, day: int, uncited: int, run: str = "", cached: int = 0
) -> None:
    """A complete, fully judged online run of the day, built like `evals online-run` makes one:
    `uncited` answers lose their citations, `cached` attempts hit the local call cache."""
    questions = _questions(day)
    traffic = tmp / "traffic" / f"day-{day:02d}.jsonl"
    traffic.write_text("".join(q.model_dump_json() + "\n" for q in questions), encoding="utf-8")
    cases = tmp / "cases" / f"online-day-{day:02d}.jsonl"
    write_suite(cases, [online_case(q) for q in questions])
    run = run or f"r{day}"
    manifest = {"suite": {"path": str(cases), "sha256": file_sha256(cases)}}
    store.start_run(
        run,
        agent="docs",
        agent_version="v",
        suite=cases.stem,
        attempts=1,
        manifest=manifest,
        online=True,
    )
    role = {"day": day, "config": "configs/default.yaml", "champion": True}
    store.annotate_run(run, "online", role | {"traffic_sha256": file_sha256(traffic)})
    store.annotate_run(run, "judge", {"version": ONLINE_JUDGE_VERSION})
    for n, q in enumerate(questions):
        usage = Usage(
            input_tokens=1000,
            output_tokens=100,
            model_calls=4,
            cached_calls=int(n < cached),
            latency_ms=5000.0,
        )
        outside = q.type == "unanswerable"
        bare = n < uncited
        cites = [] if outside or bare else [{"url": LAMBDA_URL, "quote": "x"}]
        output = {
            "answer": "" if outside else "Up to 10,240 MB.",
            "citations": cites,
            "abstained": outside,
        }
        grades = [("answer_keywords", Grade(metric="abstained", value=float(outside)))]
        grades.append(("trajectory", Grade(metric="model_calls", value=4.0)))
        if not outside:
            grades.append(("citations", Grade(metric="uncited_answer", value=float(bare))))
            if not bare:
                grades.append(("citations", Grade(metric="fabricated_citation", value=0.0)))
                grades.append(("judge", Grade(metric="judge_supported", value=float(n % 2))))
        store.record_attempt(run, q.id, 1, AgentResult(output=output, usage=usage), 0.0, grades)
    store.finish_run(run, "complete")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Six online days in a store, built once: inserts row by row are slow."""
    base = tmp_path_factory.mktemp("monitor")
    (base / "traffic").mkdir()
    (base / "cases").mkdir()
    store = ResultsStore(base / "evals.duckdb")
    try:
        for day in (1, 2, 3, 4, 5):
            _day(store, base, day, uncited=1)
        _day(store, base, 6, uncited=10)  # most answers lose their citations
    finally:
        store.close()
    return base


@pytest.fixture
def setup(built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(built, tmp_path, dirs_exist_ok=True)  # each test gets its own store
    monkeypatch.setattr(cli, "TRAFFIC_DIR", tmp_path / "traffic")
    monkeypatch.setattr(mon, "REFERENCE_PATH", tmp_path / "reference.json")
    monkeypatch.setattr(mon, "ALERTS_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(mon, "POST_HOC_PATH", tmp_path / "post-hoc.json")
    monkeypatch.setattr(mon, "JUDGE_CHECK_PATH", tmp_path / "judge-check.json")
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    return config


def _monitor(config: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, ["monitor", *args, "-c", str(config)])
    return result.exit_code, " ".join(result.output.split())


def test_monitor_freezes_a_quiet_reference_then_alerts_on_a_bad_day(setup: Path) -> None:
    code, output = _monitor(setup, "6")
    assert code == 2 and "No frozen reference yet" in output
    code, output = _monitor(setup, "--freeze")
    assert code == 0, output
    frozen = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
    assert frozen["days"] == {str(d): f"r{d}" for d in (1, 2, 3, 4, 5)}
    assert all(r["alerts"] == [] for r in frozen["leave_one_day_out"])
    code, output = _monitor(setup, "6")
    assert code == 1 and "ALERT on day 6: uncited_answer" in output
    assert json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8"))["6"]["alerts"] == [
        "uncited_answer"
    ]
    code, output = _monitor(setup, "3")
    assert code == 2 and "is a reference day" in output


def test_a_noisy_reference_is_not_frozen(setup: Path, tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a second champion run of day 5 that looks like the bad day 6
        _day(store, tmp_path, 5, uncited=10, run="r5-bad")
    finally:
        store.close()
    code, output = _monitor(setup, "--freeze")
    assert code == 2 and "2 complete online runs" in output  # which counts is not a default
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.finish_run("r5", "failed")
    finally:
        store.close()
    code, output = _monitor(setup, "--freeze")
    assert code == 1 and "too noisy" in output and not mon.REFERENCE_PATH.exists()
    code, output = _monitor(setup, "--freeze")  # a stop is a stop: no second draw by default
    assert code == 2 and "already stopped the demo" in output


def test_a_frozen_reference_is_replaced_only_on_purpose(setup: Path) -> None:
    assert _monitor(setup, "--freeze")[0] == 0
    first = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
    first["frozen_at"] = "2026-10-19T16:47:55+00:00"  # as if frozen a day earlier
    mon.REFERENCE_PATH.write_text(json.dumps(first), encoding="utf-8")
    assert _monitor(setup, "6")[0] == 1  # writes alerts.json against this reference
    for path in (mon.JUDGE_SAMPLE_PATH, mon.JUDGE_CHECK_PATH):  # a judge check on these runs
        path.write_text('{"runs": {}}', encoding="utf-8")
    code, output = _monitor(setup, "--freeze")
    assert code == 2 and "already frozen" in output
    code, output = _monitor(setup, "--freeze", "--refreeze")
    assert code == 0 and "archived" in output and not mon.ALERTS_PATH.exists()
    archived = list((mon.REFERENCE_PATH.parent / "archive").glob("*/alerts.json"))
    assert len(archived) == 1 and mon.REFERENCE_PATH.exists()
    # the same runs: the judge check belongs to them and stays, or a final score would reopen
    assert "kept judge-check-sample.json, judge-check.json" in output
    assert mon.JUDGE_SAMPLE_PATH.exists() and mon.JUDGE_CHECK_PATH.exists()
    again = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
    # the same runs re-recorded: the freeze date stands, and the first record is archived
    assert again["days"] == first["days"] and again["frozen_at"] == "2026-10-19T16:47:55+00:00"
    assert again["checked_at"] != again["frozen_at"]
    assert Path(again["previous_record"]).name == "reference.json"
    assert Path(again["previous_record"]).exists()


def test_a_refreeze_on_other_runs_archives_the_judge_check(setup: Path, tmp_path: Path) -> None:
    assert _monitor(setup, "--freeze")[0] == 0
    for path in (mon.JUDGE_SAMPLE_PATH, mon.JUDGE_CHECK_PATH):
        path.write_text('{"runs": {}}', encoding="utf-8")
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # day 5's run replaced by another quiet one
        store.finish_run("r5", "failed")
        _day(store, tmp_path, 5, uncited=1, run="r5-again")
    finally:
        store.close()
    code, output = _monitor(setup, "--freeze", "--refreeze")
    assert code == 0 and "kept" not in output
    assert not mon.JUDGE_SAMPLE_PATH.exists() and not mon.JUDGE_CHECK_PATH.exists()
    archive = mon.REFERENCE_PATH.parent / "archive"
    assert len(list(archive.glob("*/judge-check.json"))) == 1  # moved, never deleted
    frozen = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
    assert frozen["days"]["5"] == "r5-again" and frozen["frozen_at"] == frozen["checked_at"]


def test_a_day_that_cannot_be_checked_is_refused_and_one_not_cleared_says_so(
    setup: Path, tmp_path: Path
) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        _day(store, tmp_path, 7, uncited=1, cached=2)  # quiet, but latency cannot be tested
        _day(store, tmp_path, 8, uncited=1)
        store.delete_grades("r8", "judge")  # the judge never ran on day 8
    finally:
        store.close()
    assert _monitor(setup, "--freeze")[0] == 0
    code, output = _monitor(setup, "7")
    assert code == 3 and "not cleared: latency (refused: 2 attempts hit" in output
    code, output = _monitor(setup, "8")
    assert code == 2 and "no judge verdict" in output
    code, output = _monitor(setup, "8", "--run", "r6")
    assert code == 2 and "is a run of day 6, not 8" in output


def test_the_monitor_refuses_a_missing_config_or_database(setup: Path, tmp_path: Path) -> None:
    code, output = _monitor(tmp_path / "nowhere.yaml", "--freeze")
    assert code == 2 and "No config file" in output
    setup.write_text(
        f'paths:\n  db: "{(tmp_path / "missing.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    code, output = _monitor(setup, "--freeze")
    assert code == 2 and "no results database" in output
    assert not (tmp_path / "missing.duckdb").exists()  # never created as an empty database


def test_the_dashboard_shows_every_metric_and_the_alert_day(
    setup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_evals import dashboard as dash

    monkeypatch.setattr(dash, "DASHBOARD_PATH", tmp_path / "dashboard" / "index.html")
    assert _monitor(setup, "--freeze")[0] == 0
    assert _monitor(setup, "6")[0] == 1
    result = CliRunner().invoke(cli.app, ["dashboard", "-c", str(setup)])
    assert result.exit_code == 0, result.output
    page = dash.DASHBOARD_PATH.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and page.count("<svg") == 8  # 7 metrics + fabricated
    assert "Alert:" in page and "uncited_answer" in page and "day 6" in page
    assert "<script" not in page and "http" not in page.split("<main>")[0]  # static, no fetches
    assert "(geometric mean)" in page and "(log" not in page  # captions match the drawn units
    assert "a normal day of n" in page  # each day over the range a normal day of its n shows


def test_the_normal_range_is_as_wide_as_one_day_not_the_pooled_interval() -> None:
    from agent_evals import dashboard as dash

    rate = mon.ALERT_FAMILY[1]  # uncited_answer, a rate
    band = dash.normal_range(rate, [1.0, 0.0] * 250, 100, (0, 1))  # 500 values at 50%
    assert band is not None
    lo, hi = band  # one day of 100 at 50% spans about 40% to 60%; the pooled mean's +-4 points
    assert 0.38 <= lo <= 0.42 and 0.58 <= hi <= 0.62
    assert dash.normal_range(rate, [1.0, 0.0] * 250, 100, (0, 1)) == band  # seeded


def test_a_recorded_verdict_is_shown_again_never_recomputed_or_overwritten(
    setup: Path, tmp_path: Path
) -> None:
    assert _monitor(setup, "--freeze")[0] == 0
    code, output = _monitor(setup, "6")
    assert code == 1 and "ALERT on day 6: uncited_answer" in output
    recorded = mon.ALERTS_PATH.read_bytes()
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # day 6 re-graded after its verdict: checked afresh, it would be quiet
        store.delete_grades("r6", "citations")
        for q in _questions(6)[:16]:
            store.add_grades(
                "r6", q.id, 1, [("citations", Grade(metric="uncited_answer", value=0.0))]
            )
    finally:
        store.close()
    code, output = _monitor(setup, "6")
    assert code == 1 and "ALERT on day 6: uncited_answer" in output  # its recorded verdict
    assert "as recorded" in output and "and stands" in output
    assert mon.ALERTS_PATH.read_bytes() == recorded  # nothing recomputed, nothing written
    code, output = _monitor(setup, "6", "--run", "r6-again")
    assert code == 1 and "The recorded verdict is r6's, not r6-again's" in output
    code, output = _monitor(setup, "6", "--post-hoc")  # read again, into its own file only
    assert code == 0 and "Post hoc, not a verdict" in output and "day 6 shows no alert" in output
    assert "stands: uncited_answer" in output and mon.ALERTS_PATH.read_bytes() == recorded
    readings = json.loads(mon.POST_HOC_PATH.read_text(encoding="utf-8"))
    assert list(readings) == ["6"] and len(readings["6"]) == 1
    reading = readings["6"][0]
    assert reading["post_hoc"] is True and reading["run_id"] == "r6" and reading["alerts"] == []
    assert reading["recorded_verdict"]["alerts"] == ["uncited_answer"]
    assert _monitor(setup, "6", "--post-hoc")[0] == 0  # a second reading is kept beside the first
    assert len(json.loads(mon.POST_HOC_PATH.read_text(encoding="utf-8"))["6"]) == 2
    assert mon.ALERTS_PATH.read_bytes() == recorded
    setup.write_text(  # the verdict is read from its record alone, not from the database
        f'paths:\n  db: "{(tmp_path / "missing.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    code, output = _monitor(setup, "6")
    assert code == 1 and "as recorded" in output and "no results database" not in output


def test_a_post_hoc_reading_comes_only_after_a_recorded_verdict_and_is_archived_with_it(
    setup: Path,
) -> None:
    assert _monitor(setup, "--freeze")[0] == 0
    code, output = _monitor(setup, "6", "--post-hoc")
    assert code == 2 and "no recorded verdict" in output and not mon.POST_HOC_PATH.exists()
    assert not mon.ALERTS_PATH.exists()  # nor does asking for a reading record a verdict
    code, output = _monitor(setup, "--freeze", "--post-hoc")
    assert code == 2 and "--post-hoc reads one day" in output
    assert _monitor(setup, "6")[0] == 1 and _monitor(setup, "6", "--post-hoc")[0] == 0
    code, output = _monitor(setup, "--freeze", "--refreeze")  # readings of the old reference too
    assert code == 0 and not mon.POST_HOC_PATH.exists() and not mon.ALERTS_PATH.exists()
    assert len(list((mon.REFERENCE_PATH.parent / "archive").glob("*/post-hoc.json"))) == 1
    assert _monitor(setup, "6")[0] == 1  # against the new reference, day 6 is checked afresh


def test_a_post_hoc_reading_reads_the_recorded_run_unless_told_otherwise(
    setup: Path, tmp_path: Path
) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a second complete run of day 6, a quiet one
        _day(store, tmp_path, 6, uncited=1, run="r6b")
    finally:
        store.close()
    assert _monitor(setup, "--freeze")[0] == 0
    code, output = _monitor(setup, "6")
    assert code == 2 and "2 complete online runs" in output  # which counts is a decision
    assert _monitor(setup, "6", "--run", "r6")[0] == 1
    assert _monitor(setup, "6", "--post-hoc")[0] == 0  # the run the verdict was given on
    assert _monitor(setup, "6", "--post-hoc", "--run", "r6b")[0] == 0
    readings = json.loads(mon.POST_HOC_PATH.read_text(encoding="utf-8"))["6"]
    assert [(r["run_id"], r["alerts"]) for r in readings] == [
        ("r6", ["uncited_answer"]),
        ("r6b", []),
    ]
    assert json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8"))["6"]["run_id"] == "r6"


def test_a_recorded_day_exits_with_its_recorded_code(setup: Path, tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        _day(store, tmp_path, 7, uncited=1, cached=2)  # quiet, but latency cannot be tested
        _day(store, tmp_path, 8, uncited=1)  # quiet and clear
    finally:
        store.close()
    assert _monitor(setup, "--freeze")[0] == 0
    assert _monitor(setup, "7")[0] == 3 and _monitor(setup, "8")[0] == 0
    code, output = _monitor(setup, "7")
    assert code == 3 and "as recorded" in output and "not cleared: latency" in output
    code, output = _monitor(setup, "8")
    assert code == 0 and "as recorded" in output and "Day 8: no alert" in output
    alerts = json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8"))
    alerts["12"] = {"run_id": "r12", "alerts": []}  # a record that is no monitor report
    mon.ALERTS_PATH.write_text(json.dumps(alerts), encoding="utf-8")
    code, output = _monitor(setup, "12")
    assert code == 2 and "not a monitor report" in output and "not checked again" in output


def test_a_verdict_recorded_while_a_check_ran_is_not_overwritten(
    setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _monitor(setup, "--freeze")[0] == 0
    real = mon.check_day
    meanwhile = {"6": {"run_id": "r6", "alerts": [], "checked_at": "2026-10-28T00:00:00+00:00"}}

    def racing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        mon.ALERTS_PATH.write_text(json.dumps(meanwhile), encoding="utf-8")  # another command
        return real(*args, **kwargs)

    monkeypatch.setattr(mon, "check_day", racing)
    code, output = _monitor(setup, "6")
    assert code == 2 and "recorded while this check ran" in output
    assert json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8")) == meanwhile


def test_the_dashboard_hides_every_judges_sampled_answers(
    setup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_evals import dashboard as dash

    monkeypatch.setattr(dash, "DASHBOARD_PATH", tmp_path / "dashboard" / "index.html")
    assert _monitor(setup, "--freeze")[0] == 0
    runs = [f"r{d}" for d in (1, 2, 3, 4, 5)]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # another judge's check sample on the reference runs; the first check has none yet
        hidden = dash.least_supported(store, "r1", {})[0]["case"]  # shown until it is sampled
        drawn = {"reference_runs": runs, "runs": {"r1": [hidden]}, "verdict_digests": {}}
        store.annotate_runs(runs, mon.sample_record("judge-v5"), drawn)
    finally:
        store.close()
    shown: dict[str, set[str]] = {}
    real = dash.least_supported

    def spy(
        store: ResultsStore,
        run_id: str,
        traffic: dict[str, Any],
        k: int = 5,
        hidden: Collection[str] = (),
    ) -> list[dict[str, str]]:
        worst = real(store, run_id, traffic, k, hidden)
        shown[run_id] = {a["case"] for a in worst}
        return worst

    monkeypatch.setattr(dash, "least_supported", spy)
    result = CliRunner().invoke(cli.app, ["dashboard", "-c", str(setup)])
    assert result.exit_code == 0, result.output
    assert hidden not in shown["r1"] and len(shown["r1"]) == 5  # the next one takes its place
