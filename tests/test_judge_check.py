"""The judge check that lets judge_supported alert: 30 blind labels, at most 2 false passes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore
from agent_evals.types import AgentResult, Grade


def _store(tmp_path: Path, labels: dict[str, list[bool]] | None = None) -> dict[int, str]:
    """Five reference runs with 10 judged answers each (the judge passes every other one),
    plus optional labels: per run, the labeler's verdict on each sampled answer in order."""
    store = ResultsStore(tmp_path / "evals.duckdb")
    runs = {d: f"r{d}" for d in range(1, 6)}
    try:
        for run in runs.values():
            store.start_run(
                run,
                agent="docs",
                agent_version="v",
                suite="x",
                attempts=1,
                manifest={},
                online=True,
            )
            for i in range(10):
                answer = AgentResult(output={"answer": "a", "citations": [], "abstained": False})
                passed = float(i % 2 == 0)
                store.record_attempt(
                    run,
                    f"c{i}",
                    1,
                    answer,
                    0.0,
                    [("judge", Grade(metric="judge_supported", value=passed))],
                )
        if labels is not None:
            sample = mon.judge_check_sample(store, runs)
            for run, verdicts in labels.items():
                for case, supported in zip(sample[run], verdicts, strict=True):
                    store.record_label(
                        run,
                        case,
                        1,
                        "aleena",
                        supported=supported,
                        responsive=True,
                        note="",
                        label_split="judge-dev",
                    )
    finally:
        store.close()
    return runs


def test_the_sample_is_six_judged_answers_a_day_drawn_reproducibly(tmp_path: Path) -> None:
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        sample = mon.judge_check_sample(store, runs)
        assert sample == mon.judge_check_sample(store, runs)
    finally:
        store.close()
    assert set(sample) == set(runs.values()) and all(len(c) == 6 for c in sample.values())


def _score(tmp_path: Path, labels: dict[str, list[bool]]) -> dict[str, Any]:
    runs = _store(tmp_path, labels)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        return mon.judge_check_score(store, mon.judge_check_sample(store, runs), "aleena")
    finally:
        store.close()


def test_the_score_counts_false_passes_and_needs_every_label(tmp_path: Path) -> None:
    agree = {f"r{d}": [True] * 6 for d in range(1, 6)}  # she passes everything: no false pass
    result = _score(tmp_path / "a", agree)
    assert result["complete"] and result["false_passes"] == 0 and result["passed"]
    harsh = {f"r{d}": [False] * 6 for d in range(1, 6)}  # she fails everything the judge passed
    result = _score(tmp_path / "b", harsh)
    assert result["false_passes"] == sum(r["judge_supported"] for r in result["rows"]) > 2
    assert not result["passed"]
    partial = {"r1": [True] * 6}
    result = _score(tmp_path / "c", partial)
    assert not result["complete"] and not result["passed"] and result["labeled"] == 6


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: dict[int, str]) -> Path:
    for name in ("REFERENCE_PATH", "JUDGE_SAMPLE_PATH", "JUDGE_CHECK_PATH"):
        monkeypatch.setattr(mon, name, tmp_path / f"{name.lower()}.json")
    mon.REFERENCE_PATH.write_text(
        json.dumps({"days": {str(d): r for d, r in runs.items()}}), encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    return config


def test_score_records_the_result_the_monitor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path, {f"r{d}": [True] * 6 for d in range(1, 6)})
    config = _config(tmp_path, monkeypatch, runs)
    result = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    assert result.exit_code == 0 and "may now alert" in " ".join(result.output.split())
    assert mon.judge_enabled(mon.JUDGE_CHECK_PATH)
    assert json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))  # the sample was saved


def test_label_walks_the_reference_runs_and_stops_when_she_quits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    seen: list[tuple[str, list[str]]] = []

    def fake_label(**kwargs: Any) -> None:  # labels the first run in full, then "quits"
        seen.append((kwargs["run_id"], kwargs["case_ids"]))
        assert kwargs["show_judge"] is False  # blind
        if len(seen) == 1:
            store = ResultsStore(tmp_path / "evals.duckdb")
            try:
                for case in kwargs["case_ids"]:
                    store.record_label(
                        kwargs["run_id"],
                        case,
                        1,
                        "aleena",
                        supported=True,
                        responsive=True,
                        note="",
                        label_split="judge-dev",
                    )
            finally:
                store.close()

    monkeypatch.setattr(cli, "label", fake_label)
    result = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert result.exit_code == 0 and "Stopped" in result.output
    assert [run for run, _ in seen] == ["r1", "r2"]  # the second run was left unfinished
    assert all(len(cases) == 6 for _, cases in seen)


def test_the_monitor_keeps_the_judge_out_until_the_check_passes(tmp_path: Path) -> None:
    path = tmp_path / "judge-check.json"
    assert not mon.judge_enabled(path)
    path.write_text(json.dumps({"passed": False}), encoding="utf-8")
    assert not mon.judge_enabled(path)
    path.write_text(json.dumps({"passed": True}), encoding="utf-8")
    assert mon.judge_enabled(path)
