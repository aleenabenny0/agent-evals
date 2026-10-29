"""The blind days (phase 7 part 5): sealed choices, runs under them, and the reveal."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore

SEALED = {9: "subtle", 10: "unchanged", 11: "larger"}
SECRETS = [*cli.BLIND_CHOICES, "default.yaml", "retrieval-k1"]  # never shown before the reveal


@pytest.fixture
def blind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A test folder holding the three choices' configs under their own names, all on the
    test's database, so no blind command can reach the real one. Returns the default config."""
    monkeypatch.setattr(cli, "BLIND_PATH", tmp_path / "blind.json")
    monkeypatch.setattr(cli, "BLIND_REVEAL_PATH", tmp_path / "blind-reveal.json")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.chdir(tmp_path)
    for path in cli.BLIND_CHOICES.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n', encoding="utf-8"
        )
    return tmp_path / cli.DEFAULT_CONFIG


def _answers(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[dict[str, Any]]:
    """Script the prompts; record whether each was hidden."""
    seen: list[dict[str, Any]] = []
    queue = list(answers)

    def ask(prompt: str, **kwargs: Any) -> str:
        seen.append({"prompt": prompt, "hidden": kwargs.get("password", False)})
        answer = queue.pop(0)
        if kwargs.get("choices") and answer not in kwargs["choices"]:
            raise AssertionError(f"{answer!r} is not one of {kwargs['choices']}")
        return answer

    from rich.prompt import Prompt

    monkeypatch.setattr(Prompt, "ask", staticmethod(ask))
    return seen


def _seal(choices: dict[int, str] = SEALED, passphrase: str = "pw") -> list[str]:
    """Seal these choices; return the answers that repeat them, in prompt order."""
    commitment = cli._commitment(choices, passphrase)
    cli.BLIND_PATH.write_text(json.dumps({"commitment": commitment}), encoding="utf-8")
    return [choices[d] for d in cli.BLIND_DAYS] + [passphrase]


def _clean(output: str) -> str:
    """The output with its whitespace folded, once it is checked to name no choice or config."""
    out = " ".join(output.split())
    for secret in SECRETS:
        assert secret not in out, f"{secret!r} shown: {out}"
    return out


def _ran(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand in for the paid run; record what it was asked to run."""
    ran: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> str:
        ran.append(kwargs)
        return f"new-{len(ran)}"

    monkeypatch.setattr(cli, "_online_run", fake)
    return ran


def _served(tmp_path: Path, runs: dict[str, tuple[int, str]], *, shadow: str = "") -> None:
    """Complete runs (run id: day, config), each started a minute after the last one stored."""
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        row = store.con.execute("SELECT COUNT(*) FROM runs").fetchone()
        for minute, (run_id, (d, config)) in enumerate(
            runs.items(), start=int(row[0]) if row else 0
        ):
            store.start_run(
                run_id,
                agent="docs",
                agent_version="v",
                suite="x",
                attempts=1,
                manifest={},
                online=True,
            )
            role = {"day": d, "config": config, "shadow": run_id == shadow}
            store.annotate_run(run_id, "online", role)
            store.finish_run(run_id, "complete")
            store.con.execute(
                "UPDATE runs SET started_at = ? WHERE run_id = ?",
                [datetime(2026, 10, 21, 18, minute), run_id],
            )
    finally:
        store.close()


def test_seal_keeps_only_the_hash_and_stands(blind: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw", "pw"])
    result = CliRunner().invoke(cli.app, ["blind", "seal"])
    assert result.exit_code == 0, result.output
    assert all(s["hidden"] for s in seen) and len(seen) == 5  # nothing typed in the clear
    sealed = json.loads(cli.BLIND_PATH.read_text(encoding="utf-8"))
    assert sealed["commitment"] == cli._commitment(
        {9: "subtle", 10: "unchanged", 11: "larger"}, "pw"
    )
    # the options and their expectations are public; the per-day choices are not kept
    assert set(sealed) == {"sealed_at", "days", "options", "expected_alerts", "commitment"}
    assert CliRunner().invoke(cli.app, ["blind", "seal"]).exit_code == 2  # a seal stands
    # forgotten: --again replaces a seal no blind day has run under, archiving the old one
    _answers(monkeypatch, ["larger", "larger", "unchanged", "new", "new"])
    again = CliRunner().invoke(cli.app, ["blind", "seal", "--again", "-c", str(blind)])
    assert again.exit_code == 0, again.output
    assert len(list((cli.BLIND_PATH.parent / "archive").glob("*/blind.json"))) == 1
    resealed = json.loads(cli.BLIND_PATH.read_text(encoding="utf-8"))
    assert resealed["commitment"] == cli._commitment(
        {9: "larger", 10: "larger", 11: "unchanged"}, "new"
    )
    _verdicts(cli.BLIND_PATH.parent, {9: "configs/default.yaml"}, {9: []})  # day 9 ran
    _answers(monkeypatch, ["larger", "larger", "unchanged", "new", "new"])
    again = CliRunner().invoke(cli.app, ["blind", "seal", "--again", "-c", str(blind)])
    assert again.exit_code == 2 and "already ran under the seal" in " ".join(again.output.split())
    assert json.loads(cli.BLIND_PATH.read_text(encoding="utf-8")) == resealed  # untouched
    cli.BLIND_PATH.unlink()
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw", "other"])
    result = CliRunner().invoke(cli.app, ["blind", "seal"])
    assert result.exit_code == 2 and not cli.BLIND_PATH.exists()  # passphrases differ
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert CliRunner().invoke(cli.app, ["blind", "seal"]).exit_code == 2


def test_run_checks_the_seal_before_anything_is_spent(
    blind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _ran(monkeypatch)
    result = CliRunner().invoke(cli.app, ["blind", "run", "9"])
    assert result.exit_code == 2 and "Seal the choices first" in result.output
    cli.BLIND_PATH.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["blind", "run", "9"])
    assert result.exit_code == 2 and "holds no commitment" in " ".join(result.output.split())
    answers = _seal()
    result = CliRunner().invoke(cli.app, ["blind", "run", "12"])
    assert result.exit_code == 2 and "The blind days are" in result.output
    _answers(monkeypatch, [*answers[:-1], "wrong"])
    result = CliRunner().invoke(cli.app, ["blind", "run", "9"])
    assert result.exit_code == 2 and "do not match the seal" in _clean(result.output)
    _answers(monkeypatch, ["larger", "unchanged", "larger", "pw"])  # a valid choice, not day 9's
    result = CliRunner().invoke(cli.app, ["blind", "run", "9"])
    assert result.exit_code == 2 and "do not match the seal" in _clean(result.output)
    assert not ran  # a mistyped choice runs nothing
    seen = _answers(monkeypatch, answers)
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert all(s["hidden"] for s in seen) and len(seen) == 4  # nothing typed in the clear
    assert ran == [
        {"day": 9, "judge_answers": False, "shadow": False, "config": cli.BLIND_CHOICES["subtle"]}
    ]
    out = _clean(result.output)
    assert "evals monitor 9" in out and "--run" not in out  # the verdict comes next
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert CliRunner().invoke(cli.app, ["blind", "run", "9"]).exit_code == 2


def test_run_refuses_an_unpinned_judge_in_any_choice_before_anything_is_typed(
    blind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _ran(monkeypatch)
    answers = _seal()  # day 9: subtle
    larger = cli.BLIND_CHOICES["larger"]  # not day 9's choice, and its judge has no pin
    larger.write_text(
        larger.read_text(encoding="utf-8") + "judge:\n  grader_name: judge-v2.1\n",
        encoding="utf-8",
    )
    seen = _answers(monkeypatch, [])
    result = CliRunner().invoke(cli.app, ["blind", "run", "9"])
    out = _clean(result.output)  # it names no choice and no config
    assert result.exit_code == 2 and "judge-v2.1, has no pin" in out
    assert "Nothing was typed or run" in out and not seen and not ran
    seen = _answers(monkeypatch, answers)  # without the judge, the day runs as before
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 4 and [r["config"] for r in ran] == [cli.BLIND_CHOICES["subtle"]]


def test_run_refuses_only_a_day_that_ran_under_its_sealed_choice(
    blind: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _ran(monkeypatch)
    answers = _seal()  # day 10: unchanged
    # a shadow is not a run of the day, and a run under another choice cannot be the day's
    _served(
        tmp_path,
        {"shadow": (10, "configs/default.yaml"), "mistyped": (10, "configs/retrieval-k1.yaml")},
        shadow="shadow",
    )
    _answers(monkeypatch, answers)
    result = CliRunner().invoke(cli.app, ["blind", "run", "10"])
    assert result.exit_code == 0, result.output
    assert [r["config"] for r in ran] == [cli.BLIND_CHOICES["unchanged"]]
    assert "evals monitor 10 --run new-1" in _clean(result.output)  # the day has two runs now
    _served(tmp_path, {"sealed": (10, "configs/default.yaml")})
    _answers(monkeypatch, answers)
    result = CliRunner().invoke(cli.app, ["blind", "run", "10"])
    out = _clean(result.output)
    assert result.exit_code == 2 and "already ran under its sealed choice (sealed)" in out
    assert "evals monitor 10 --run sealed" in out and len(ran) == 1


def test_a_database_in_use_is_refused_before_anything_secret_is_typed(
    blind: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal()
    _served(tmp_path, {"r": (10, "configs/default.yaml")})
    seen = _answers(monkeypatch, [])
    writer = ResultsStore(tmp_path / "evals.duckdb")  # another command, writing
    try:
        for command in (["blind", "run", "10"], ["blind", "match", "10"]):
            result = CliRunner().invoke(cli.app, command)
            assert result.exit_code == 2, result.output
            assert "in use by another command" in " ".join(result.output.split())
    finally:
        writer.close()
    assert not seen


def test_match_names_only_the_first_run_under_the_sealed_choice(
    blind: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = _seal()  # day 10: unchanged
    match = ["blind", "match", "10"]
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 2 and "no complete run yet" in result.output
    _served(
        tmp_path,
        {
            "run-a": (10, "configs/retrieval-k1.yaml"),
            "run-b": (10, "configs/default.yaml"),
            "run-c": (10, "configs/default.yaml"),  # a replay of run-b
            "run-d": (9, "configs/default.yaml"),
        },
    )
    _answers(monkeypatch, [*answers[:-1], "wrong"])
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 2 and "do not match the seal" in _clean(result.output)
    seen = _answers(monkeypatch, answers)
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 0, result.output
    assert all(s["hidden"] for s in seen) and len(seen) == 4  # nothing typed in the clear
    out = _clean(result.output)
    assert "Day 10's run is run-b" in out and "evals monitor 10 --run run-b" in out
    assert not any(run in out for run in ("run-a", "run-c", "run-d"))  # nor how the rest compare
    _answers(monkeypatch, _seal({9: "subtle", 10: "larger", 11: "larger"}))  # none ran under it
    result = CliRunner().invoke(cli.app, match)
    out = _clean(result.output)
    assert result.exit_code == 1 and "No run of day 10 ran under its sealed choice" in out
    assert "evals blind run 10" in out  # the way on, which runs it under the sealed choice
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert CliRunner().invoke(cli.app, match).exit_code == 2


def test_online_run_serves_no_blind_day_and_shadows_one_only_after_its_verdict(
    blind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _ran(monkeypatch)
    result = CliRunner().invoke(cli.app, ["online-run", "10", "--no-judge"])
    out = " ".join(result.output.split())
    assert result.exit_code == 2 and "only uv run evals blind run 10 serves it" in out
    shadow = ["online-run", "10", "--shadow", "--no-judge"]
    result = CliRunner().invoke(cli.app, shadow)
    assert result.exit_code == 2 and "evals monitor 10 first" in " ".join(result.output.split())
    assert not ran
    mon.ALERTS_PATH.write_text(json.dumps({"10": {"run_id": "r", "alerts": []}}), encoding="utf-8")
    result = CliRunner().invoke(cli.app, shadow)
    assert result.exit_code == 0, result.output
    assert ran == [
        {"day": 10, "judge_answers": False, "shadow": True, "config": cli.DEFAULT_CONFIG}
    ]


def _verdicts(tmp_path: Path, configs: dict[int, str], alerts: dict[int, list[str]]) -> None:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        for d, config in configs.items():
            store.start_run(
                f"r{d}",
                agent="docs",
                agent_version="v",
                suite="x",
                attempts=1,
                manifest={},
                online=True,
            )
            store.annotate_run(f"r{d}", "online", {"day": d, "config": config})
    finally:
        store.close()
    mon.ALERTS_PATH.write_text(
        json.dumps({str(d): {"run_id": f"r{d}", "alerts": a} for d, a in alerts.items()}),
        encoding="utf-8",
    )


def test_reveal_waits_for_every_verdict_then_checks_the_seal_and_the_runs(
    blind: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw", "pw"])
    assert CliRunner().invoke(cli.app, ["blind", "seal"]).exit_code == 0
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw"])
    result = CliRunner().invoke(cli.app, ["blind", "reveal", "-c", str(blind)])
    assert result.exit_code == 2 and "no verdict yet" in " ".join(result.output.split())
    configs = {
        9: "configs/retrieval-k1.yaml",
        10: "configs/default.yaml",
        11: "configs/retrieval-k1-short.yaml",
    }
    _verdicts(tmp_path, configs, {9: [], 10: [], 11: ["model_calls", "error"]})
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "wrong"])
    result = CliRunner().invoke(cli.app, ["blind", "reveal", "-c", str(blind)])
    assert result.exit_code == 2 and "do not match the seal" in " ".join(result.output.split())
    seen = _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw"])
    result = CliRunner().invoke(cli.app, ["blind", "reveal", "-c", str(blind)])
    assert result.exit_code == 0, result.output
    assert [s["hidden"] for s in seen] == [False, False, False, True]  # choices now in the clear
    revealed = json.loads(cli.BLIND_REVEAL_PATH.read_text(encoding="utf-8"))
    assert [row["outcome"] for row in revealed["days"]] == [
        "missed",
        "quiet, as expected",
        "caught",
    ]
    assert all(row["config_matches"] for row in revealed["days"])
    # a day that did not run under its sealed choice voids the reveal
    _verdicts(
        tmp_path / "void",
        configs | {10: "configs/retrieval-k1.yaml"},
        {9: [], 10: ["error"], 11: []},
    )
    blind.write_text(
        f'paths:\n  db: "{(tmp_path / "void" / "evals.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw"])
    result = CliRunner().invoke(cli.app, ["blind", "reveal", "-c", str(blind)])
    assert result.exit_code == 1 and "did not run under its sealed choice" in " ".join(
        result.output.split()
    )
    assert [
        row["outcome"] for row in json.loads(cli.BLIND_REVEAL_PATH.read_text("utf-8"))["days"]
    ] == [
        "missed",
        "false alarm",
        "missed",
    ]
