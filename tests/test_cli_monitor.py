"""`evals monitor` end to end on a synthetic store: freeze a quiet reference, catch a bad day."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

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
    assert _monitor(setup, "6")[0] == 1  # writes alerts.json against this reference
    code, output = _monitor(setup, "--freeze")
    assert code == 2 and "already frozen" in output
    code, output = _monitor(setup, "--freeze", "--refreeze")
    assert code == 0 and "archived" in output and not mon.ALERTS_PATH.exists()
    archived = list((mon.REFERENCE_PATH.parent / "archive").glob("*/alerts.json"))
    assert len(archived) == 1 and mon.REFERENCE_PATH.exists()


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
