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

from agent_evals import cli
from agent_evals import dashboard as dash
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore
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
    return config


def _reference_days(monkeypatch: pytest.MonkeyPatch, days: list[mon.DayValues]) -> None:
    """The reference runs' values, as `load_day` would read them, for the noise check that
    `score` reruns with judge_supported in the family."""
    by_run = {f"r{d}": v for d, v in enumerate(days, 1)}
    monkeypatch.setattr(mon, "load_day", lambda store, run, traffic_dir: by_run[run])


def _cli_draw(config: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Draw and save the sample as `judge-check label` does (she quits at once); return it."""
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


def test_score_never_draws_and_no_draw_follows_her_labels(
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
    try:  # a record under another labeler's name puts nothing in
        kept = mon.recorded_judge_check(store, runs.values())
        assert kept is not None
        for run in runs.values():
            store.annotate_run(run, mon.JUDGE_CHECK_RECORD, kept | {"labeler": "bob"})
    finally:
        store.close()
    assert not _enabled(tmp_path, runs.values())
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:  # back to her record, then the sampled answers re-judged after the pass
        for run in runs.values():
            store.annotate_run(run, mon.JUDGE_CHECK_RECORD, kept)
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
    _label_sample(tmp_path, sample, supported=False)  # she fails all 15 judge passes
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


def test_a_null_stdin_is_not_a_person_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    with open(os.devnull) as null:  # on Windows the NUL device reports isatty() as well
        monkeypatch.setattr(sys, "stdin", null)
        assert not cli._interactive()
