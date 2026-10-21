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
    result = CliRunner().invoke(cli.app, ["blind", "run", "9", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert seen[0]["hidden"] and ran == [
        {"day": 9, "judge_answers": False, "shadow": False, "config": cli.BLIND_CHOICES["subtle"]}
    ]
    assert "evals monitor 9" in " ".join(result.output.split())  # the verdict comes next


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
