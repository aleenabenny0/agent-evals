"""The code agent's case model and the machine checks a case must pass (phase 3, part 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.agents.code.checks import check_case
from agent_evals.agents.code.sandbox import InProcessRunner
from agent_evals.suites import load_suite, write_suite
from agent_evals.types import Case

TASK = 'Create an SQS queue named "orders" whose messages stay hidden for 60 seconds after a read.'
TEST = """
sqs = session.client("sqs")
url = sqs.get_queue_url(QueueName="orders")["QueueUrl"]
attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["VisibilityTimeout"])["Attributes"]
assert attrs["VisibilityTimeout"] == "60"
"""
REFERENCE = """
def solve(session):
    session.client("sqs").create_queue(QueueName="orders", Attributes={"VisibilityTimeout": "60"})
"""
NEAR_MISS = """
def solve(session):
    session.client("sqs").create_queue(QueueName="orders")
"""


def code_case(**changes: Any) -> dict[str, Any]:
    task = {"services": ["sqs"], "test": TEST, "reference": REFERENCE, "near_miss": NEAR_MISS}
    task |= changes.pop("task", {})
    return {
        "id": "code-sqs-001",
        "suite": "capability",
        "agent": "code",
        "type": "create",
        "question": TASK,
        "task": task,
        "split": "unassigned",
        "provenance": "synthetic",
        "area": "integration",
    } | changes


def test_a_code_case_has_a_task_and_no_gold() -> None:
    case = Case.model_validate(code_case())
    assert case.task is not None and case.gold is None and case.is_draft
    with pytest.raises(ValidationError, match="a code case"):
        Case.model_validate(code_case(gold={"answer": "x", "must_include": [["x"]]}))
    with pytest.raises(ValidationError, match="a code case"):
        Case.model_validate(code_case(agent="docs"))
    with pytest.raises(ValidationError, match="a code case"):
        Case.model_validate(code_case() | {"task": None})


def test_a_possible_task_needs_its_test_reference_and_near_miss() -> None:
    with pytest.raises(ValidationError, match="a possible task"):
        Case.model_validate(code_case(task={"near_miss": None}))
    with pytest.raises(ValidationError, match="a possible task"):
        Case.model_validate(code_case(task={"impossible_because": "no reason"}))


def test_an_impossible_task_says_why_and_has_nothing_to_run() -> None:
    impossible = {
        "test": None,
        "reference": None,
        "near_miss": None,
        "impossible_because": "SQS has no setting that encrypts a queue with a password.",
    }
    case = Case.model_validate(code_case(type="unanswerable", task=impossible))
    assert not case.answerable
    with pytest.raises(ValidationError, match="an impossible task"):
        Case.model_validate(code_case(type="unanswerable"))


def test_a_good_case_passes_every_machine_check() -> None:
    held = check_case(Case.model_validate(code_case()), InProcessRunner())
    assert held.ok, held.errors
    assert held.runs["reference"].ok
    assert held.runs["nothing"].stage == held.runs["near_miss"].stage == "test"


def test_a_test_the_near_miss_passes_does_not_see_the_task() -> None:
    weak = TEST.replace('assert attrs["VisibilityTimeout"] == "60"', "assert url")
    held = check_case(Case.model_validate(code_case(task={"test": weak})), InProcessRunner())
    assert any("near miss passes the test" in e for e in held.errors)


def test_a_broken_setup_is_the_case_not_the_code() -> None:
    held = check_case(
        Case.model_validate(code_case(task={"setup": "raise RuntimeError('typo')"})),
        InProcessRunner(),
    )
    assert held.errors == ["the setup fails: RuntimeError: typo"]


def test_a_test_that_crashes_on_the_near_miss_does_not_count_as_seeing_the_task() -> None:
    # A test that errors on the near miss, as indexing a key it never set would, proves less
    # than one that asserts on the setting the task asks for.
    crashing = TEST.replace(
        'assert attrs["VisibilityTimeout"] == "60"',
        'if attrs["VisibilityTimeout"] != "60":\n    raise KeyError("VisibilityTimeout")',
    )
    case = Case.model_validate(code_case(task={"test": crashing}))
    held = check_case(case, InProcessRunner())
    assert held.runs["near_miss"].error == "KeyError: 'VisibilityTimeout'"
    assert any("crashes the test instead of failing it" in e for e in held.errors)


def test_a_near_miss_that_crashes_is_a_weak_check() -> None:
    crash = "def solve(session):\n    raise ValueError('oops')\n"
    held = check_case(Case.model_validate(code_case(task={"near_miss": crash})), InProcessRunner())
    assert any("the near miss fails at solve" in e for e in held.errors)


def test_a_reference_that_calls_what_does_not_exist_is_caught_before_it_runs() -> None:
    wrong = REFERENCE.replace('Attributes={"VisibilityTimeout": "60"}', "VisibilityTimeout=60")
    held = check_case(Case.model_validate(code_case(task={"reference": wrong})), InProcessRunner())
    assert (
        "the reference calls what does not exist: sqs.create_queue: VisibilityTimeout"
        in held.errors
    )


# ---- validate and review -----------------------------------------------------------------------


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commands' sandbox, swapped for the in-process runner: these cases are the tests' own."""
    from agent_evals.agents.code import sandbox

    monkeypatch.setattr(sandbox, "image_available", lambda tag=None: True)
    monkeypatch.setattr(sandbox, "DockerSandbox", lambda: InProcessRunner())
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def write_cases(tmp_path: Path, *cases: dict[str, Any]) -> Path:
    path = tmp_path / "code.drafts.jsonl"
    write_suite(path, [Case.model_validate(c) for c in cases])
    return path


def test_validate_runs_the_machine_checks_of_a_code_suite(tmp_path: Path, no_docker: None) -> None:
    good = write_cases(tmp_path, code_case())
    result = CliRunner().invoke(cli.app, ["validate", str(good)])
    assert result.exit_code == 0, result.output
    assert "machine checks: 1 of 1 cases pass" in result.output
    weak = TEST.replace('assert attrs["VisibilityTimeout"] == "60"', "assert url")
    bad = write_cases(tmp_path, code_case(task={"test": weak}))
    result = CliRunner().invoke(cli.app, ["validate", str(bad)])
    assert result.exit_code == 1 and "the near miss passes the test" in result.output


def test_review_accepts_a_code_case_that_passed_its_checks(tmp_path: Path, no_docker: None) -> None:
    path = write_cases(tmp_path, code_case())
    args = ["review", str(path), "--reviewer", "author"]
    result = CliRunner().invoke(cli.app, args, input="a\n")
    assert result.exit_code == 0, result.output
    assert "Does the test check what the task says" in result.output
    assert load_suite(path)[0].verified_by == "author"


def test_review_cannot_accept_a_code_case_that_failed_its_checks(
    tmp_path: Path, no_docker: None
) -> None:
    weak = TEST.replace('assert attrs["VisibilityTimeout"] == "60"', "assert url")
    path = write_cases(tmp_path, code_case(task={"test": weak}))
    result = CliRunner().invoke(
        cli.app, ["review", str(path), "--reviewer", "author"], input="a\ns\n"
    )
    assert result.exit_code == 0, result.output
    assert "machine check failed" in result.output and "(r)eject, (s)kip, (q)uit" in result.output
    assert load_suite(path)[0].is_draft
