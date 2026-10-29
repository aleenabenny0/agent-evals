"""The judge check that lets judge_supported alert: 30 blind labels, at most 2 false passes."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli, online
from agent_evals import dashboard as dash
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore, check_record_key, is_write_once
from agent_evals.types import AgentResult, Grade
from test_monitor import REFERENCE
from test_monitor import _day as synthetic_day
from test_monitor import _ones as ones


def _draw(store: ResultsStore, runs: dict[int, str]) -> dict[str, list[str]]:
    """The sample exactly as `judge-check` draws it: the dashboard's answers left out."""
    return mon.judge_check_sample(store, runs, dash.shown_with_verdicts(store, runs.values()))


def _store(tmp_path: Path, labels: dict[str, list[bool]] | None = None) -> dict[int, str]:
    """Five reference runs with 16 judged answers each (the judge passes every other one, so 8
    fails remain 3 after the dashboard's 5 least supported are left out), plus optional
    labels: per run, the labeler's verdict on each sampled answer in order."""
    store = ResultsStore(tmp_path / "evals.duckdb")
    runs = {d: f"r{d}" for d in range(1, 6)}
    try:
        for day, run in runs.items():  # each its day's one complete champion run, as frozen
            store.start_run(
                run,
                agent="docs",
                agent_version="v",
                suite="x",
                attempts=1,
                manifest={},
                online=True,
            )
            store.annotate_run(run, "online", {"day": day, "champion": True})
            store.finish_run(run, "complete")
            for i in range(16):
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
            sample = _draw(store, runs)
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
        sample = _draw(store, runs)
        assert sample == _draw(store, runs)
        passed = {
            run: sum(store.grades_for(run, "judge")[(c, 1)]["judge_supported"] for c in cases)
            for run, cases in sample.items()
        }
    finally:
        store.close()
    assert set(sample) == set(runs.values()) and all(len(c) == 6 for c in sample.values())
    assert set(passed.values()) == {3}  # 15 judge passes in all, where false passes can occur
    assert all(cases == sorted(cases) for cases in sample.values())  # order shows no verdict


def _enabled(tmp_path: Path, runs: Iterable[str]) -> bool:
    store = ResultsStore(tmp_path / "evals.duckdb", read_only=True)
    try:
        return mon.judge_enabled(store, runs)
    finally:
        store.close()


def _score(tmp_path: Path, labels: dict[str, list[bool]]) -> dict[str, Any]:
    runs = _store(tmp_path, labels)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        return mon.judge_check_score(store, _draw(store, runs), "aleena")
    finally:
        store.close()


def test_the_score_counts_false_passes_and_needs_every_label(tmp_path: Path) -> None:
    agree = {f"r{d}": [True] * 6 for d in range(1, 6)}  # the labeler passes all: no false pass
    result = _score(tmp_path / "a", agree)
    assert result["complete"] and result["false_passes"] == 0 and result["labels_passed"]
    assert result["judge_passed"] == 15 and result["false_fails"] == 15  # reported, no bar
    harsh = {f"r{d}": [False] * 6 for d in range(1, 6)}  # the labeler fails all the judge passed
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
        sample = _draw(store, runs)
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
    _reference_days(monkeypatch, REFERENCE)  # score loads the days first; a test may swap them
    return config


def _reference_days(monkeypatch: pytest.MonkeyPatch, days: list[mon.DayValues]) -> None:
    """The reference runs' values, as `load_day` would read them, for the noise check that
    `score` reruns with judge_supported in the family."""
    by_run = {f"r{d}": v for d, v in enumerate(days, 1)}
    monkeypatch.setattr(mon, "load_day", lambda store, run, traffic_dir: by_run[run])


def _cli_draw(config: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Draw and save the sample as `judge-check label` does (quitting at once); return it."""
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    drew = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert drew.exit_code == 0 and "Stopped" in drew.output, drew.output
    return dict(json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))["runs"])


def _label_sample(tmp_path: Path, sample: dict[str, list[str]], supported: bool) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        for run, cases in sample.items():
            for case in cases:
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


def _cli_score(config: Path) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, ["judge-check", "score", "-c", str(config)])
    return result.exit_code, " ".join(result.output.split())


def test_score_records_the_result_the_monitor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    _label_sample(tmp_path, _cli_draw(config, monkeypatch), supported=True)
    _reference_days(monkeypatch, REFERENCE)
    code, output = _cli_score(config)
    assert code == 0 and "may now alert" in output
    assert _enabled(tmp_path, runs.values())
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # the first complete result is kept in the database as well
        kept = mon.recorded_judge_check(store, runs.values())
    finally:
        store.close()
    assert kept == json.loads(mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8"))


def test_the_judge_stays_out_if_the_reference_is_noisy_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    _label_sample(tmp_path, _cli_draw(config, monkeypatch), supported=True)
    odd = synthetic_day("odd", values={"judge_supported": ones(80, 60)})
    _reference_days(monkeypatch, [*REFERENCE[:4], odd])
    code, output = _cli_score(config)
    assert code == 0 and "the labels pass" in output and "too noisy" in output
    assert not _enabled(tmp_path, runs.values())


def test_score_never_draws_and_no_draw_follows_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    code, output = _cli_score(config)
    assert code == 2 and "No judge-check sample yet" in output
    assert not mon.JUDGE_SAMPLE_PATH.exists()
    _label_sample(tmp_path, {"r1": ["c1"]}, supported=True)  # a label, but the sample is gone
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    result = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert result.exit_code == 2 and "would not be blind" in " ".join(result.output.split())
    assert not mon.JUDGE_SAMPLE_PATH.exists()


def test_the_judges_verdicts_stay_hidden_until_the_check_is_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)
    case = sample["r1"][0]
    validate = ["judge-validate", "r1", "--split", "all", "-c", str(config)]
    refused = CliRunner().invoke(cli.app, validate)
    assert refused.exit_code == 2 and "labeled blind" in " ".join(refused.output.split())
    trace = CliRunner().invoke(cli.app, ["trace", "r1", case, "-c", str(config)])
    assert trace.exit_code == 0 and "judge_supported" not in trace.output
    assert "held back" in " ".join(trace.output.split())
    other = CliRunner().invoke(cli.app, ["trace", "r1", "c15", "-c", str(config)])
    assert "c15" not in sample["r1"] and "judge_supported" in other.output  # not sampled
    _label_sample(tmp_path, sample, supported=True)
    _reference_days(monkeypatch, REFERENCE)
    assert _cli_score(config)[0] == 0
    assert CliRunner().invoke(cli.app, validate).exit_code == 0  # scored: nothing left to hide
    shown = CliRunner().invoke(cli.app, ["trace", "r1", case, "-c", str(config)])
    assert "judge_supported" in shown.output


def test_label_walks_the_reference_runs_and_stops_when_the_labeler_quits(
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


def test_only_a_recorded_pass_on_unchanged_grades_puts_the_judge_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)
    assert not _enabled(tmp_path, runs.values())  # drawn, not scored
    forged = {"complete": True, "passed": True, "labeler": "aleena"}
    forged["reference_runs"] = sorted(runs.values())
    mon.JUDGE_CHECK_PATH.write_text(json.dumps(forged), encoding="utf-8")
    assert not _enabled(tmp_path, runs.values())  # a file counts for nothing
    code, output = _cli_score(config)
    assert code == 1 and "0 of 30 labeled" in output  # nor does it make a final score
    _label_sample(tmp_path, sample, supported=True)
    _reference_days(monkeypatch, REFERENCE)
    assert _cli_score(config)[0] == 0 and _enabled(tmp_path, runs.values())
    assert not _enabled(tmp_path, ["r1", "r2", "r3", "r4", "other"])  # another reference
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # the store itself refuses to change a written record, whoever asks
        kept = mon.recorded_judge_check(store, runs.values())
        assert kept is not None
        with pytest.raises(ValueError, match="written once"):
            store.annotate_run("r1", mon.JUDGE_CHECK_RECORD, kept | {"labeler": "bob"})
        store.annotate_run("r1", mon.JUDGE_CHECK_RECORD, kept)  # the same record: a no-op
    finally:
        store.close()
    assert _enabled(tmp_path, runs.values())

    def manifests(record: dict[str, Any]) -> None:  # a raw write, past the store's guard
        store = ResultsStore(tmp_path / "evals.duckdb")
        try:
            for run in runs.values():
                manifest = store.run_info(run)["manifest"] | {mon.JUDGE_CHECK_RECORD: record}
                store.con.execute(
                    "UPDATE runs SET manifest = ? WHERE run_id = ?", [json.dumps(manifest), run]
                )
        finally:
            store.close()

    manifests(kept | {"labeler": "bob"})  # a record under another labeler puts nothing in
    assert not _enabled(tmp_path, runs.values())
    manifests(kept)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # the real record back, then the sampled answers re-judged after the pass
        case = sample["r1"][0]
        store.delete_grades("r1", "judge", [(case, 1)])
        store.add_grades("r1", case, 1, [("judge", Grade(metric="judge_supported", value=0.5))])
    finally:
        store.close()
    assert not _enabled(tmp_path, runs.values())  # its grades are not the ones it was checked on
    no_option = CliRunner().invoke(cli.app, ["judge-check", "score", "--labeler", "bob"])
    assert no_option.exit_code == 2  # the command has no --labeler at all


def test_the_first_complete_score_is_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)
    _label_sample(tmp_path, sample, supported=False)  # the labeler fails all 15 judge passes
    _reference_days(monkeypatch, REFERENCE)
    code, output = _cli_score(config)
    assert code == 0 and "it fails" in output
    recorded = mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8")
    assert json.loads(recorded)["scored_at"] and not json.loads(recorded)["passed"]
    # the result shows the judge's verdicts; labels changed after reading them must not count
    _label_sample(tmp_path, sample, supported=True)
    code, output = _cli_score(config)
    assert code == 0 and "first complete result is final" in output
    assert mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8") == recorded  # untouched
    mon.JUDGE_CHECK_PATH.unlink()  # nor can deleting (or editing) the file reopen it
    code, output = _cli_score(config)
    assert code == 0 and "did not pass" in output and "restored from the results database" in output
    assert json.loads(mon.JUDGE_CHECK_PATH.read_text(encoding="utf-8")) == json.loads(recorded)
    assert not _enabled(tmp_path, runs.values())


def test_a_re_judged_sample_is_refused_before_labeling_and_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)  # the labeler quits at once
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


def test_the_judges_view_is_refused_and_a_moved_sample_file_lifts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)
    published = mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8")
    mon.JUDGE_SAMPLE_PATH.unlink()  # the blind is kept in the results database, not the file
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    args = ["label", "r1", "--labeler", "aleena", "--revisit", "all", "--split", "judge-dev"]
    result = CliRunner().invoke(cli.app, [*args, "--show-judge", "-c", str(config)])
    assert result.exit_code == 2 and "labeled blind" in " ".join(result.output.split())
    other = tmp_path / "other-judge.yaml"  # a config naming its judge otherwise
    other.write_text(config.read_text(encoding="utf-8") + "judge:\n  grader_name: judge-v4\n")
    trace = CliRunner().invoke(cli.app, ["trace", "r1", sample["r1"][0], "-c", str(other)])
    assert trace.exit_code == 0 and "judge_supported" not in trace.output
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    again = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert again.exit_code == 0 and "restored from the results database" in " ".join(
        again.output.split()
    )
    assert mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8") == published  # the same sample


def test_answers_already_shown_with_a_verdict_are_never_sampled(tmp_path: Path) -> None:
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        free = mon.judge_check_sample(store, runs)
        shown = {run: cases[:2] for run, cases in free.items()}  # e.g. on the dashboard
        sample = mon.judge_check_sample(store, runs, shown)
        drawn = _draw(store, runs)  # as the command draws it
        published = dash.shown_with_verdicts(store, runs.values())
    finally:
        store.close()
    assert all(not set(shown[run]) & set(cases) for run, cases in sample.items())
    assert all(len(cases) == 6 for cases in sample.values())  # still 3 and 3 from the rest
    assert all(len(published[run]) == 5 for run in drawn)  # the dashboard's 5 a day
    assert all(not set(published[run]) & set(cases) for run, cases in drawn.items())


def test_the_check_runs_only_on_the_frozen_reference_and_its_records_are_written_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    frozen = mon.REFERENCE_PATH.read_text(encoding="utf-8")
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # another complete online run, not day 5's champion
        store.start_run(
            "x5", agent="docs", agent_version="v", suite="x", attempts=1, manifest={}, online=True
        )
        store.annotate_run("x5", "online", {"day": 5, "champion": False})
        store.finish_run("x5", "complete")
    finally:
        store.close()
    edited = {"days": {str(d): r for d, r in runs.items()} | {"5": "x5"}}
    mon.REFERENCE_PATH.write_text(json.dumps(edited), encoding="utf-8")
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    result = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    assert result.exit_code == 2 and "not that day's complete champion online run" in " ".join(
        result.output.split()
    )
    mon.REFERENCE_PATH.write_text(frozen, encoding="utf-8")
    _cli_draw(config, monkeypatch)  # the real draw, recorded on r1 to r5
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # day 5 rerun: a new reference that shares r1 to r4 with the drawn one
        store.finish_run("r5", "failed")
        store.annotate_run("x5", "online", {"day": 5, "champion": True})
    finally:
        store.close()
    mon.REFERENCE_PATH.write_text(json.dumps(edited), encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    output = " ".join(result.output.split())
    assert result.exit_code == 2 and "already belongs to the judge check of another" in output
    store = ResultsStore(tmp_path / "evals.duckdb", read_only=True)
    try:  # the real draw's record is untouched on every run
        assert mon.recorded_sample(store, runs.values()) is not None
    finally:
        store.close()


def test_a_forged_result_file_is_moved_aside_and_any_label_blocks_a_draw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    _label_sample(tmp_path, {"r2": ["c1"]}, supported=True)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # the same label again under another spelling of the labeler's name
        for label in store.labels("r2"):
            store.record_label(
                "r2",
                label["case_id"],
                1,
                "Aleena",
                supported=True,
                responsive=True,
                note="",
                label_split="judge-dev",
            )
        store.con.execute("DELETE FROM labels WHERE labeler = 'aleena'")
    finally:
        store.close()
    forged = {"complete": True, "passed": True, "labeler": "aleena"}
    mon.JUDGE_CHECK_PATH.write_text(json.dumps(forged), encoding="utf-8")
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    result = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    output = " ".join(result.output.split())
    assert (
        "matched no record in the results database" in output and not mon.JUDGE_CHECK_PATH.exists()
    )
    assert list((mon.REFERENCE_PATH.parent / "archive").glob(f"*/{mon.JUDGE_CHECK_PATH.name}"))
    assert result.exit_code == 2 and "already have labels" in output  # anyone's, any name
    assert not mon.JUDGE_SAMPLE_PATH.exists()


def test_the_reason_the_judge_is_out_is_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)

    def standing() -> str:
        store = ResultsStore(tmp_path / "evals.duckdb", read_only=True)
        try:
            return mon.judge_standing(store, runs.values())[1]
        finally:
            store.close()

    assert standing() == mon.PENDING
    _label_sample(tmp_path, sample, supported=True)
    _reference_days(monkeypatch, REFERENCE)
    assert _cli_score(config)[0] == 0 and standing().startswith("in the family")
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a re-judge of one sampled answer after the pass
        case = sample["r2"][0]
        store.delete_grades("r2", "judge", [(case, 1)])
        store.add_grades("r2", case, 1, [("judge", Grade(metric="judge_supported", value=0.5))])
    finally:
        store.close()
    assert "re-judged after its check" in standing()
    html = dash.render([], {"days": {}}, False, standing())
    assert "re-judged after its check" in html and "until the judge passes" not in html


def test_the_store_writes_the_check_records_once_and_all_together(tmp_path: Path) -> None:
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.annotate_run("r1", "judge_check_sample", {"a": 1})
        store.annotate_run("r1", "judge_check_sample", {"a": 1})  # the same: allowed
        with pytest.raises(ValueError, match="written once"):
            store.annotate_run("r1", "judge_check_sample", {"a": 2})
        store.annotate_run("r1", "online", {"day": 9})  # other entries stay writable
        with pytest.raises(ValueError, match="written once"):  # r1 fails: r2 is rolled back
            store.annotate_runs(["r2", "r1"], "judge_check_sample", {"a": 3})
        assert "judge_check_sample" not in store.run_info("r2")["manifest"]
    finally:
        store.close()
    assert runs


def test_a_judge_named_like_another_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    for name in ("judge_check_sample", "judge_check", "online"):
        other = tmp_path / f"{name}.yaml"
        other.write_text(config.read_text(encoding="utf-8") + f"judge:\n  grader_name: {name}\n")
        result = CliRunner().invoke(cli.app, ["judge", "r1", "--limit", "0", "-c", str(other)])
        assert result.exit_code == 2 and "Nothing was judged" in " ".join(result.output.split())


def test_a_replay_of_a_reference_day_stays_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    sample = _cli_draw(config, monkeypatch)
    case = sample["r1"][0]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a free cache replay: the same case ids and the same verdicts in a new run
        store.start_run(
            "replay", agent="docs", agent_version="v", suite="x", attempts=1, manifest={}
        )
        for (c, attempt), grades in store.grades_for("r1", "judge").items():
            answer = AgentResult(output={"answer": "a", "citations": [], "abstained": False})
            graded = [("judge", Grade(metric=m, value=v)) for m, v in grades.items()]
            store.record_attempt("replay", c, attempt, answer, 0.0, graded)
    finally:
        store.close()
    trace = CliRunner().invoke(cli.app, ["trace", "replay", case, "-c", str(config)])
    assert trace.exit_code == 0 and "judge_supported" not in trace.output
    compare = ["compare", "r1", "replay", "--metric", "judge_supported", "-c", str(config)]
    refused = CliRunner().invoke(cli.app, compare)
    assert refused.exit_code == 2 and "labeled blind" in " ".join(refused.output.split())
    probe = CliRunner().invoke(cli.app, ["judge-probe", "replay", "-c", str(config)])
    assert probe.exit_code == 2 and "labeled blind" in " ".join(probe.output.split())


def test_a_score_refusal_never_depends_on_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for supported in (True, False):
        base = tmp_path / str(supported)
        base.mkdir()
        runs = _store(base)
        config = _config(base, monkeypatch, runs)
        _label_sample(base, _cli_draw(config, monkeypatch), supported=supported)

        def moved(store: ResultsStore, run: str, traffic_dir: Path) -> mon.DayValues:
            raise ValueError(f"{run}: its traffic file is missing or not the one it ran")

        monkeypatch.setattr(mon, "load_day", moved)
        code, output = _cli_score(config)
        assert code == 2 and "traffic file" in output and not mon.JUDGE_CHECK_PATH.exists()
        store = ResultsStore(base / "evals.duckdb", read_only=True)
        try:  # nothing was recorded, whichever way the labels went
            assert mon.recorded_judge_check(store, runs.values()) is None
        finally:
            store.close()


def test_a_reference_day_cannot_run_again_and_a_drawn_check_survives_a_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    result = CliRunner().invoke(cli.app, ["online-run", "1", "--no-judge", "-c", str(config)])
    assert result.exit_code == 2 and "frozen reference day" in " ".join(result.output.split())
    sample = _cli_draw(config, monkeypatch)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # a second complete champion run of day 1 all the same (from before the rule)
        store.start_run(
            "r1b", agent="docs", agent_version="v", suite="x", attempts=1, manifest={}, online=True
        )
        store.annotate_run("r1b", "online", {"day": 1, "champion": True})
        store.finish_run("r1b", "complete")
    finally:
        store.close()
    assert _cli_draw(config, monkeypatch) == sample  # the drawn check carries on
    _label_sample(tmp_path, sample, supported=True)
    assert _cli_score(config)[0] == 0


def test_a_draw_cut_short_is_completed_and_stray_copies_are_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    mon.JUDGE_SAMPLE_PATH.write_text('{"runs": {}}', encoding="utf-8")  # a copy of nothing
    sample = _cli_draw(config, monkeypatch)  # moves the stray copy aside, then draws
    assert list((mon.REFERENCE_PATH.parent / "archive").glob(f"*/{mon.JUDGE_SAMPLE_PATH.name}"))
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # as if the draw had died after its first write
        for run in ("r3", "r4", "r5"):
            manifest = store.run_info(run)["manifest"]
            manifest.pop(mon.JUDGE_SAMPLE_RECORD)
            store.con.execute(
                "UPDATE runs SET manifest = ? WHERE run_id = ?", [json.dumps(manifest), run]
            )
    finally:
        store.close()
    mon.JUDGE_SAMPLE_PATH.unlink()
    monkeypatch.setattr(cli, "label", lambda **kwargs: None)
    again = CliRunner().invoke(cli.app, ["judge-check", "label", "-c", str(config)])
    output = " ".join(again.output.split())
    assert again.exit_code == 0 and "copied to r3, r4, r5" in output and "restored" in output
    store = ResultsStore(tmp_path / "evals.duckdb", read_only=True)
    try:
        assert all(mon.JUDGE_SAMPLE_RECORD in store.run_info(r)["manifest"] for r in runs.values())
    finally:
        store.close()
    assert json.loads(mon.JUDGE_SAMPLE_PATH.read_text(encoding="utf-8"))["runs"] == sample


def test_a_null_stdin_is_not_a_person_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    with open(os.devnull) as null:  # on Windows the NUL device reports isatty() as well
        monkeypatch.setattr(sys, "stdin", null)
        assert not cli._interactive()


def test_each_judges_check_records_have_their_own_keys_and_are_written_once(
    tmp_path: Path,
) -> None:
    # the first check, the validated judge's, keeps its names; another judge's carry its name
    assert (mon.sample_record(), mon.check_record()) == ("judge_check_sample", "judge_check")
    assert mon.sample_record("judge-v5") == "judge_check_sample:judge-v5"
    assert mon.check_record("judge-v5") == "judge_check:judge-v5"
    records = ("judge_check", "judge_check_sample", "judge_check:judge-v5")
    assert all(is_write_once(k) for k in (*records, "judge_check_sample:judge-v5"))
    assert not any(is_write_once(k) for k in ("judge", "judge-v5", "online", "judge_checks"))
    with pytest.raises(ValueError, match="not a judge-check record"):
        check_record_key("online", "judge-v5")
    runs = _store(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        key = mon.sample_record("judge-v5")
        store.annotate_run("r1", key, {"a": 1})
        store.annotate_run("r1", key, {"a": 1})  # the same: allowed
        with pytest.raises(ValueError, match="written once"):
            store.annotate_run("r1", key, {"a": 2})
        store.annotate_run("r1", mon.sample_record(), {"b": 1})  # the first check's, beside it
        assert store.run_info("r1")["manifest"][key] == {"a": 1}
        scored = mon.check_record("judge-v5")
        store.annotate_run("r1", scored, {"c": 1})
        with pytest.raises(ValueError, match="written once"):  # r1 fails: r2 is rolled back
            store.annotate_runs(["r2", "r1"], scored, {"c": 2})
        assert scored not in store.run_info("r2")["manifest"]
    finally:
        store.close()
    assert runs


def test_a_judge_named_like_another_judges_check_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    for n, name in enumerate((mon.sample_record("judge-v5"), mon.check_record("judge-v5"))):
        other = tmp_path / f"record-{n}.yaml"  # a colon cannot be in a Windows file name
        other.write_text(config.read_text(encoding="utf-8") + f"judge:\n  grader_name: '{name}'\n")
        result = CliRunner().invoke(cli.app, ["judge", "r1", "--limit", "0", "-c", str(other)])
        assert result.exit_code == 2 and "Nothing was judged" in " ".join(result.output.split())


def test_a_run_holds_one_check_per_judge_and_each_judge_stands_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _store(tmp_path)
    config = _config(tmp_path, monkeypatch, runs)
    first = _cli_draw(config, monkeypatch)  # the first check, the validated judge's: passed
    _label_sample(tmp_path, first, supported=True)
    _reference_days(monkeypatch, REFERENCE)
    assert _cli_score(config)[0] == 0 and _enabled(tmp_path, runs.values())
    reference = sorted(runs.values())
    v5 = "judge-v5"
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # the claim judge grades the same answers under its own name, then draws its sample
        for run in reference:
            for (case, attempt), grades in store.grades_for(run, "judge").items():
                verdict = Grade(
                    metric="judge_supported", value=grades["judge_supported"], reason=case
                )
                store.add_grades(run, case, attempt, [(v5, verdict)])
        mine = {
            r: [c for c in (f"c{i}" for i in range(16)) if c not in first[r]][:4] for r in reference
        }
        drawn = {
            "drawn_at": "2026-11-02T00:00:00+00:00",
            "reference_runs": reference,
            "runs": mine,
            "verdict_digests": mon.verdict_digests(store, mine, v5),
        }
        assert mon.foreign_record(store, reference, mon.sample_record(v5)) is None
        store.annotate_runs(reference, mon.sample_record(v5), drawn)  # beside the first check's
        sample = mon.recorded_sample(store, reference)
        assert sample is not None and sample["runs"] == first  # the first check's, untouched
        assert mon.recorded_sample(store, reference, v5) == drawn
        assert mon.recorded_judge_check(store, reference, v5) is None
        pending = (False, "pending: judge-v5 has not yet passed its blind labels")
        assert mon.judge_standing(store, reference, v5) == pending
        pins = {"judge": online.ONLINE_JUDGE_VERSION, v5: "abcdefabcdef"}
        monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", pins)  # pinned: it may grade online
        assert mon.judge_standing(store, reference, v5) == pending  # but that puts it in nothing
        assert mon.judge_standing(store, reference)[0]  # v2 stays in on its own check
        # its sample is held back, though the first check is scored, and kept off the dashboard
        assert mon.pending_blind(store) == {c for cases in mine.values() for c in cases}
        assert set(mine["r1"]) <= mon.blind_cases(store, "r1")  # case ids repeat across runs here
        assert mon.sampled_cases(store, reference)["r1"] == set(first["r1"]) | set(mine["r1"])
        # another reference that shares r1 to r4 can take neither judge's records
        other = [*reference[:4], "x5"]
        assert mon.foreign_record(store, other, mon.sample_record(v5)) == "r1"
        assert mon.foreign_record(store, other, mon.sample_record()) == "r1"
    finally:
        store.close()
    trace = CliRunner().invoke(cli.app, ["trace", "r1", mine["r1"][0], "-c", str(config)])
    assert trace.exit_code == 0 and "judge_supported" not in trace.output  # neither judge's
    validate = ["judge-validate", "r1", "--split", "all", "-c", str(config)]
    assert CliRunner().invoke(cli.app, validate).exit_code == 2
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        result = {
            "labeler": mon.JUDGE_CHECK_LABELER,
            "reference_runs": reference,
            "complete": True,
            "labels_passed": True,
            "passed": True,
        }
        store.annotate_runs(reference, mon.check_record(v5), result)
        assert mon.judge_standing(store, reference, v5) == (
            True,
            "in the family: judge-v5 passed its blind labels",
        )
        assert mon.pending_blind(store) == set()  # its own score releases its own sample
        assert mon.blind_cases(store, "r1") == set()
        with pytest.raises(ValueError, match="written once"):
            store.annotate_run("r1", mon.check_record(v5), result | {"passed": False})
        case = mine["r2"][0]  # one of its sampled answers re-judged after its check
        store.delete_grades("r2", v5, [(case, 1)])
        store.add_grades("r2", case, 1, [(v5, Grade(metric="judge_supported", value=0.5))])
        assert "re-judged after its check" in mon.judge_standing(store, reference, v5)[1]
        assert mon.judge_standing(store, reference)[0]  # v2's digests read v2's grades only
    finally:
        store.close()
