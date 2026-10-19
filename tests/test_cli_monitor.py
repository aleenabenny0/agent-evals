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
from agent_evals.online import online_case
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


def _day(store: ResultsStore, tmp: Path, day: int, uncited: int, run: str = "") -> None:
    questions = _questions(day)
    (tmp / "traffic" / f"day-{day:02d}.jsonl").write_text(
        "".join(q.model_dump_json() + "\n" for q in questions), encoding="utf-8"
    )
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
    store.annotate_run(
        run, "online", {"day": day, "config": "configs/default.yaml", "champion": True}
    )
    usage = Usage(input_tokens=1000, output_tokens=100, model_calls=4, latency_ms=5000.0)
    for n, q in enumerate(questions):
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
    assert code == 2 and "is a reference run" in output


def test_a_noisy_reference_is_not_frozen(setup: Path, tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a newer champion run of day 5 that looks like the bad day 6
        _day(store, tmp_path, 5, uncited=10, run="r5-bad")
    finally:
        store.close()
    code, output = _monitor(setup, "--freeze")
    assert code == 1 and "too noisy" in output and not mon.REFERENCE_PATH.exists()
