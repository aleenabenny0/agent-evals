"""`evals pilot-review` end to end through the command line, with scripted keyboard input."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.corpus import Corpus
from agent_evals.simulate import (
    CASE_TYPE,
    PERSONAS,
    PILOT_PER_PERSONA,
    TrafficQuestion,
    load_verdicts,
)
from conftest import LAMBDA_URL

MARKUP_QUESTION = "lambda says [error] Task timed out [/dim] after 3 seconds, why?"
ANSWER_ONE = "y\ny\n\n"  # realistic, answerable, no note


def _questions() -> list[TrafficQuestion]:
    out: list[TrafficQuestion] = []
    for persona in PERSONAS:
        for intent, n in PILOT_PER_PERSONA.items():
            for k in range(n):
                outside = intent == "outside_snapshot"
                out.append(
                    TrafficQuestion(
                        id=f"online-d02-{len(out):03d}",
                        day=2,
                        persona=persona,
                        intent=intent,
                        type=CASE_TYPE[intent],
                        question=MARKUP_QUESTION
                        if not out
                        else f"How long can a Lambda function run, {persona} {intent} {k}?",
                        source_url=None if outside else LAMBDA_URL,
                        service="Braket" if outside else None,
                    )
                )
    return out


@pytest.fixture
def pilot_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: Corpus) -> Path:
    """A snapshot of the fixture pages, a config pointing at it, and 30 questions for day 2."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "pages.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in corpus.pages.values()), encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(f'paths:\n  corpus_dir: "{snapshot.as_posix()}"\n', encoding="utf-8")
    traffic = tmp_path / "online" / "traffic"
    traffic.mkdir(parents=True)
    (traffic / "day-02.jsonl").write_text(
        "".join(q.model_dump_json() + "\n" for q in _questions()), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "TRAFFIC_DIR", traffic)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    return config


def _review(config: Path, keys: str, day: str = "2") -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, ["pilot-review", day, "-c", str(config)], input=keys)
    return result.exit_code, " ".join(result.output.split())  # rich wraps at 80 columns


def _answers() -> dict[str, dict[str, object]]:
    return load_verdicts(cli.TRAFFIC_DIR.parent / "pilot-day-02.jsonl")


def test_the_day_is_an_argument_answers_are_saved_and_a_rerun_resumes(pilot_config: Path) -> None:
    code, first = _review(pilot_config, ANSWER_ONE + "q\n")
    assert code == 0, first
    assert len(_answers()) == 1 and "Not finished: 1 of 30 reviewed" in first
    code, second = _review(pilot_config, "n\nn\n\n" * 29)  # resumes with the other 29
    assert code == 1, second  # 1 of 30 realistic: below two thirds
    assert second.count("simulated question") == 29
    assert "Fewer than two thirds" in second and len(_answers()) == 30
    # Shown exactly as generated: rich markup would drop "[error]" and crash on "[/dim]".
    assert MARKUP_QUESTION in first + second


def test_a_complete_pilot_above_the_bar_says_so(pilot_config: Path) -> None:
    code, output = _review(pilot_config, ANSWER_ONE * 30)
    assert code == 0, output
    assert "Pilot complete" in output and len(_answers()) == 30


def test_q_quits_from_any_prompt_and_saves_only_whole_answers(pilot_config: Path) -> None:
    assert _review(pilot_config, "y\nq\n")[0] == 0  # quit at the second question: nothing saved
    assert not _answers()
    code, output = _review(pilot_config, "y\nn\nq\n")  # q at the note: saved, then quit
    assert code == 0, output
    (saved,) = _answers().values()
    assert saved["answerable"] == "n" and saved["note"] == ""


def test_answers_in_upper_case_are_accepted(pilot_config: Path) -> None:
    code, output = _review(pilot_config, "Y\nN\n\nq\n")
    assert code == 0, output
    (saved,) = _answers().values()
    assert saved["realistic"] is True and saved["answerable"] == "n"


def test_an_answer_typed_after_a_torn_line_survives(pilot_config: Path) -> None:
    assert _review(pilot_config, "y\ny\none\ny\ny\ntwo\nq\n")[0] == 0
    path = cli.TRAFFIC_DIR.parent / "pilot-day-02.jsonl"
    path.write_bytes(path.read_bytes()[:-25])  # an interrupted write: the last line is torn
    assert _review(pilot_config, "n\nn\nA\nn\nn\nB\nq\n")[0] == 0
    assert sorted(str(a["note"]) for a in _answers().values()) == ["A", "B", "one"]


def test_plain_messages_for_a_missing_day_or_the_wrong_folder(
    pilot_config: Path, tmp_path: Path
) -> None:
    code, output = _review(pilot_config, "", day="5")
    assert code == 2 and "uv run evals simulate 5" in output
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text('paths:\n  corpus_dir: "no/such/snapshot"\n', encoding="utf-8")
    code, output = _review(elsewhere, "")
    assert code == 2 and "project folder" in output and "cd " in output
    assert "Traceback" not in output


def test_redo_asks_chosen_questions_again_and_the_newer_answer_wins(pilot_config: Path) -> None:
    assert _review(pilot_config, ANSWER_ONE * 30)[0] == 0
    ids = ["online-d02-005", "online-d02-011"]  # two outside-snapshot questions
    args = ["pilot-review", "2", "--redo", ",".join(ids), "-c", str(pilot_config)]
    result = CliRunner().invoke(cli.app, args, input="y\nn\nchanged\n" * 2)
    output = " ".join(result.output.split())
    assert result.exit_code == 0, output
    assert output.count("simulated question") == 2 and "Your earlier answer" in output
    answers = _answers()
    assert len(answers) == 30
    assert all(answers[i]["answerable"] == "n" and answers[i]["note"] == "changed" for i in ids)
    assert sum(1 for a in answers.values() if a["note"] == "changed") == 2
    bad = ["pilot-review", "2", "--redo", "online-d02-999", "-c", str(pilot_config)]
    result = CliRunner().invoke(cli.app, bad, input="")
    assert result.exit_code == 2 and "Not in the pilot sample" in result.output


def test_pilot_review_refuses_without_a_terminal(
    pilot_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    code, output = _review(pilot_config, "")
    assert code == 2 and "interactive terminal" in output
