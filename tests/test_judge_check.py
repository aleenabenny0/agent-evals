"""The judge check that lets judge_supported alert: 30 blind labels, at most 2 false passes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore
from agent_evals.types import AgentResult, Grade
from test_monitor import REFERENCE
from test_monitor import _day as synthetic_day
from test_monitor import _ones as ones


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


def test_the_sample_is_three_passed_and_three_failed_a_day_drawn_reproducibly(
    tmp_path: Path,
) -> None:
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        sample = mon.judge_check_sample(store, runs)
        assert sample == mon.judge_check_sample(store, runs)
        passed = {
            run: sum(store.grades_for(run, "judge")[(c, 1)]["judge_supported"] for c in cases)
            for run, cases in sample.items()
        }
    finally:
        store.close()
    assert set(sample) == set(runs.values()) and all(len(c) == 6 for c in sample.values())
    assert set(passed.values()) == {3}  # 15 judge passes in all, where false passes can occur
    assert all(cases == sorted(cases) for cases in sample.values())  # order shows no verdict


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
    assert result["complete"] and result["false_passes"] == 0 and result["labels_passed"]
    assert result["judge_passed"] == 15 and result["false_fails"] == 15  # reported, no bar
    harsh = {f"r{d}": [False] * 6 for d in range(1, 6)}  # she fails everything the judge passed
    result = _score(tmp_path / "b", harsh)
    assert result["false_passes"] == sum(r["judge_supported"] for r in result["rows"]) == 15
    assert not result["labels_passed"] and result["false_fails"] == 0
    partial = {"r1": [True] * 6}
    result = _score(tmp_path / "c", partial)
    assert not result["complete"] and not result["labels_passed"] and result["labeled"] == 6


def test_a_lost_verdict_or_a_thin_day_is_refused_not_counted(tmp_path: Path) -> None:
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        sample = mon.judge_check_sample(store, runs)
        store.delete_grades("r2", "judge", [(sample["r2"][0], 1)])  # e.g. a --replace cut short
        with pytest.raises(ValueError, match="no judge verdict"):
            mon.judge_check_score(store, sample, "aleena")  # never read as a judge fail
        # the judge re-run after the draw turns one of day 1's sampled passes into a fail
        flipped = next(c for c in sample["r1"] if mon._verdicts(store, "r1")[c])
        store.delete_grades("r1", "judge", [(flipped, 1)])
        store.add_grades("r1", flipped, 1, [("judge", Grade(metric="judge_supported", value=0.0))])
        with pytest.raises(ValueError, match="verdicts changed after the sample was drawn"):
            mon.judge_check_score(store, sample, "aleena")
        store.delete_grades("r3", "judge")
        with pytest.raises(ValueError, match="r3 has 0 judge-passed and 0 judge-failed"):
            mon.judge_check_sample(store, runs)  # 24 answers would pass as the check
        with pytest.raises(ValueError, match="6 answers from each"):
            mon.judge_check_score(store, {"r1": sample["r1"]}, "aleena")
    finally:
        store.close()


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


def _reference_days(monkeypatch: pytest.MonkeyPatch, days: list[mon.DayValues]) -> None:
    """The reference runs' values, as `load_day` would read them, for the noise check that
    `score` reruns with judge_supported in the family."""
    by_run = {f"r{d}": v for d, v in enumerate(days, 1)}
    monkeypatch.setattr(mon, "load_day", lambda store, run, traffic_dir: by_run[run])


def test_score_records_the_result_the_monitor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path, {f"r{d}": [True] * 6 for d in range(1, 6)})
    config = _config(tmp_path, monkeypatch, runs)
    _reference_days(monkeypatch, REFERENCE)
    result = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    assert result.exit_code == 0 and "may now alert" in " ".join(result.output.split())
    assert mon.judge_enabled(mon.JUDGE_CHECK_PATH, runs.values())
    assert not mon.judge_enabled(mon.JUDGE_CHECK_PATH, ["r1", "r2", "r3", "r4", "other"])
    assert json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))  # the sample was saved


def test_the_judge_stays_out_if_the_reference_is_noisy_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path, {f"r{d}": [True] * 6 for d in range(1, 6)})
    config = _config(tmp_path, monkeypatch, runs)
    odd = synthetic_day("odd", values={"judge_supported": ones(80, 60)})
    _reference_days(monkeypatch, [*REFERENCE[:4], odd])
    result = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    output = " ".join(result.output.split())
    assert result.exit_code == 0 and "the labels pass" in output and "too noisy" in output
    assert not mon.judge_enabled(mon.JUDGE_CHECK_PATH, runs.values())


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
    path, runs = tmp_path / "judge-check.json", ["r1", "r2"]
    assert not mon.judge_enabled(path, runs)
    result: dict[str, Any] = {"passed": False, "reference_runs": runs, "labeler": "aleena"}
    path.write_text(json.dumps(result), encoding="utf-8")
    assert not mon.judge_enabled(path, runs)
    path.write_text(json.dumps(result | {"passed": True}), encoding="utf-8")
    assert mon.judge_enabled(path, runs) and not mon.judge_enabled(path, ["r1", "r3"])
    path.write_text(json.dumps(result | {"passed": True, "labeler": "bob"}), encoding="utf-8")
    assert not mon.judge_enabled(path, runs)  # the plan: Aleena's labels, nobody else's
    no_option = CliRunner().invoke(cli.app, ["judge-check", "score", "--labeler", "bob"])
    assert no_option.exit_code == 2  # the command has no --labeler at all


def test_the_first_complete_score_is_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = _store(tmp_path, {f"r{d}": [True] * 6 for d in range(1, 6)})
    config = _config(tmp_path, monkeypatch, runs)
    _reference_days(monkeypatch, REFERENCE)
    first = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    assert first.exit_code == 0 and "may now alert" in " ".join(first.output.split())
    recorded = mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8")
    assert json.loads(recorded)["scored_at"]
    # the result shows the judge's verdicts; a label changed after reading them must not count
    sample = json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))["runs"]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.record_label(
            "r1",
            sample["r1"][0],
            1,
            "aleena",
            supported=False,
            responsive=True,
            note="",
            label_split="judge-dev",
        )
    finally:
        store.close()
    second = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    assert second.exit_code == 0 and "first complete result is final" in " ".join(
        second.output.split()
    )
    assert mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8") == recorded  # untouched


def test_a_re_judged_sample_is_refused_before_labeling_and_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)  # she quits at once
    drew = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert drew.exit_code == 0 and "Stopped" in drew.output  # the sample is drawn and saved
    saved = json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))
    assert saved["drawn_at"] and set(saved["verdict_digests"]) == set(runs.values())
    sample = saved["runs"]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # swap one pass and one fail on day 3: still 3 and 3, so only the digest sees it
        verdicts = mon._verdicts(store, "r3")
        passed = next(c for c in sample["r3"] if verdicts[c])
        failed = next(c for c in sample["r3"] if not verdicts[c])
        for case, value in ((passed, 0.0), (failed, 1.0)):
            store.delete_grades("r3", "judge", [(case, 1)])
            store.add_grades(
                "r3", case, 1, [("judge", Grade(metric="judge_supported", value=value))]
            )
    finally:
        store.close()
    for action in ("label", "score"):
        result = CliRunner().invoke(cli.app, ["judge-check", action, "-c", str(config)])
        output = " ".join(result.output.split())
        assert result.exit_code == 2 and "changed since the sample was drawn" in output


def test_the_judges_verdicts_stay_hidden_on_the_checks_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    mon.JUDGE_SAMPLE_PATH.write_text(json.dumps({"runs": {"r1": ["c0"]}}), encoding="utf-8")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    args = ["label", "r1", "--labeler", "aleena", "--revisit", "all", "--split", "judge-dev"]
    result = CliRunner().invoke(cli.app, [*args, "--show-judge", "-c", str(config)])
    assert result.exit_code == 2 and "labeled blind" in " ".join(result.output.split())


def test_a_null_stdin_is_not_a_person_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    with open(os.devnull) as null:  # on Windows the NUL device reports isatty() as well
        monkeypatch.setattr(sys, "stdin", null)
        assert not cli._interactive()
