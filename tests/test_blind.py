"""The blind days (phase 7 part 5): sealed choices, runs under them, and the reveal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals import monitor as mon
from agent_evals.store.db import ResultsStore


@pytest.fixture
def blind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "BLIND_PATH", tmp_path / "blind.json")
    monkeypatch.setattr(cli, "BLIND_REVEAL_PATH", tmp_path / "blind-reveal.json")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n', encoding="utf-8"
    )
    return config


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


def test_run_needs_a_seal_and_runs_the_choices_config(
    blind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "online_run", lambda **kwargs: ran.append(kwargs))
    _answers(monkeypatch, ["subtle"])
    assert CliRunner().invoke(cli.app, ["blind", "run", "9"]).exit_code == 2  # seal first
    cli.BLIND_PATH.write_text('{"commitment": "x"}', encoding="utf-8")
    assert CliRunner().invoke(cli.app, ["blind", "run", "12"]).exit_code == 2  # not a blind day
    seen = _answers(monkeypatch, ["subtle"])
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "--no-judge", "-c", str(blind)])
    assert result.exit_code == 0, result.output
    assert seen[0]["hidden"] and ran == [
        {"day": 9, "judge_answers": False, "shadow": False, "config": cli.BLIND_CHOICES["subtle"]}
    ]
    assert "evals monitor 9" in " ".join(result.output.split())  # the verdict comes next


def _served(tmp_path: Path, runs: dict[str, tuple[int, str]], *, shadow: str = "") -> None:
    """Complete served runs (run id: day, config), started a minute apart in this order."""
    from datetime import datetime

    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        for minute, (run_id, (d, config)) in enumerate(runs.items()):
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


def test_run_refuses_a_day_that_already_ran(blind: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "online_run", lambda **kwargs: ran.append(kwargs))
    cli.BLIND_PATH.write_text('{"commitment": "x"}', encoding="utf-8")
    _served(
        blind.parent,
        {"shadow10": (10, "configs/default.yaml"), "a9": (9, "configs/default.yaml")},
        shadow="shadow10",
    )
    seen = _answers(monkeypatch, ["subtle"])
    result = CliRunner().invoke(cli.app, ["blind", "run", "10", "-c", str(blind)])
    assert result.exit_code == 0, result.output  # a shadow is not a run of the day
    assert len(ran) == 1 and seen
    seen = _answers(monkeypatch, ["subtle"])
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "-c", str(blind)])
    assert result.exit_code == 2 and "already ran (a9)" in " ".join(result.output.split())
    assert len(ran) == 1 and not seen  # refused before the choice is asked
    _served(blind.parent, {"b9": (9, "configs/default.yaml")})
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "-c", str(blind)])
    assert result.exit_code == 2 and "evals blind match 9" in " ".join(result.output.split())


def test_match_names_the_first_run_under_the_sealed_choice_and_nothing_else(
    blind: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw", "pw"])
    assert CliRunner().invoke(cli.app, ["blind", "seal"]).exit_code == 0
    match = ["blind", "match", "10", "-c", str(blind)]
    assert CliRunner().invoke(cli.app, match).exit_code == 2  # no run of the day yet
    _served(
        blind.parent,
        {
            "first": (10, "configs/retrieval-k1.yaml"),
            "second": (10, "configs/default.yaml"),
            "replay": (10, "configs/default.yaml"),
            "other-day": (9, "configs/default.yaml"),
        },
    )
    _answers(monkeypatch, ["subtle", "unchanged", "larger", "wrong"])
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 2 and "do not match the seal" in " ".join(result.output.split())
    seen = _answers(monkeypatch, ["subtle", "unchanged", "larger", "pw"])
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 0, result.output
    assert all(s["hidden"] for s in seen) and len(seen) == 4  # nothing typed in the clear
    out = " ".join(result.output.split())
    assert "Day 10's run is second" in out and "evals monitor 10 --run second" in out
    assert "other-day" not in out
    for secret in [*cli.BLIND_CHOICES, "default.yaml", "retrieval-k1"]:  # never a choice or config
        assert secret not in out
    rows = [line.split() for line in result.output.splitlines() if line.strip().startswith("│")]
    assert [(row[1], row[-2]) for row in rows if row[1] in ("first", "second", "replay")] == [
        ("first", "no"),
        ("second", "yes"),
        ("replay", "yes"),
    ]
    other = cli._commitment({9: "unchanged", 10: "larger", 11: "larger"}, "pw")  # no run matches
    cli.BLIND_PATH.write_text(json.dumps({"commitment": other}), encoding="utf-8")
    _answers(monkeypatch, ["unchanged", "larger", "larger", "pw"])
    result = CliRunner().invoke(cli.app, match)
    assert result.exit_code == 1 and "No run of day 10 ran under" in " ".join(result.output.split())
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert CliRunner().invoke(cli.app, match).exit_code == 2


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
