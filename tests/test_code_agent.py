"""The code agent's loop, its tools and its graders, offline (phase 3, parts 1 and 2).

The model is scripted and the code runs in this process on the tests' own task, so nothing here
calls the API or needs Docker.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.agents.code.agent import ClosedBookCodeAgent, CodeAgent, tool_specs
from agent_evals.agents.code.prompts import RUN_LIMIT_V1
from agent_evals.agents.code.sandbox import InProcessRunner
from agent_evals.config import CodeAgentConfig
from agent_evals.contracts import CodeAnswer, contract_problems
from agent_evals.graders.code import CodeApiGrader, CodeTaskGrader
from agent_evals.types import AgentResult, Case, RunContext
from conftest import ScriptedLLM, tool_use
from test_code_cases import NEAR_MISS, REFERENCE, code_case

CTX = RunContext(run_id="r", case_id="code-sqs-001", attempt=1)
SUBMIT = {"code": REFERENCE, "abstained": False, "explanation": "Creates the queue."}
IMPOSSIBLE = {
    "type": "unanswerable",
    "task": {
        "test": None,
        "reference": None,
        "near_miss": None,
        "impossible_because": "SQS has no password setting.",
    },
}


def case(**changes: Any) -> Case:
    return Case.model_validate(code_case(**changes))


def agent(script: list[list[dict[str, Any]]], **config: Any) -> tuple[CodeAgent, ScriptedLLM]:
    llm = ScriptedLLM(script)
    return CodeAgent(llm, InProcessRunner(), CodeAgentConfig(**config)), llm


def last_tool_result(llm: ScriptedLLM) -> dict[str, Any]:
    result: dict[str, Any] = llm.requests[-1]["messages"][-1]["content"][0]
    return result


# ---- the loop ----------------------------------------------------------------------------------


def test_the_agent_looks_up_runs_and_submits() -> None:
    a, llm = agent(
        [
            [tool_use("describe_operation", {"service": "sqs", "operation": "create_queue"})],
            [tool_use("run_code", {"code": REFERENCE}, "toolu_2")],
            [tool_use("submit_answer", SUBMIT, "toolu_3")],
        ]
    )
    result = a.run(case(), CTX)
    assert result.error is None and result.output == SUBMIT
    assert [s.name for s in result.trajectory if s.kind == "tool"] == [
        "describe_operation",
        "run_code",
        "submit_answer",
    ]
    described = llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert "* QueueName: string" in described and "Attributes" in described
    ran = last_tool_result(llm)
    assert ran["is_error"] is False and "ok: True" in ran["content"]


def test_a_run_sees_the_setup_but_never_the_hidden_test() -> None:
    probe = "def solve(session):\n    return session.client('sqs').list_queues().get('QueueUrls')\n"
    setup = 'session.client("sqs").create_queue(QueueName="existing")'
    a, llm = agent(
        [[tool_use("run_code", {"code": probe})], [tool_use("submit_answer", SUBMIT, "toolu_2")]]
    )
    a.run(case(task={"setup": setup}), CTX)
    ran = last_tool_result(llm)["content"]
    assert "existing" in ran and "AssertionError" not in ran


def test_a_failed_run_comes_back_as_an_error_the_model_can_fix() -> None:
    broken = (
        "def solve(session):\n    session.client('sqs').create_queue(QueueName='orders', Bad=1)\n"
    )
    a, llm = agent(
        [[tool_use("run_code", {"code": broken})], [tool_use("submit_answer", SUBMIT, "toolu_2")]]
    )
    a.run(case(), CTX)
    ran = last_tool_result(llm)
    assert ran["is_error"] is True and "ParamValidationError" in ran["content"]


def test_runs_stop_at_the_limit() -> None:
    script = [[tool_use("run_code", {"code": REFERENCE}, f"toolu_{i}")] for i in range(3)]
    a, llm = agent([*script, [tool_use("submit_answer", SUBMIT, "toolu_9")]], max_runs=2)
    result = a.run(case(), CTX)
    assert result.error is None
    assert last_tool_result(llm)["content"] == RUN_LIMIT_V1


def test_a_lookup_that_misses_names_the_closest_operations() -> None:
    a, llm = agent(
        [
            [tool_use("describe_operation", {"service": "sqs", "operation": "create_queu"})],
            [tool_use("submit_answer", SUBMIT, "toolu_2")],
        ]
    )
    a.run(case(), CTX)
    missed = last_tool_result(llm)
    assert missed["is_error"] is True and "create_queue" in missed["content"]


@pytest.mark.parametrize(
    ("submitted", "says"),
    [
        ({"code": "x = 1", "abstained": False, "explanation": ""}, "does not define"),
        ({"code": "def solve(:", "abstained": False, "explanation": ""}, "does not parse"),
        ({"code": REFERENCE, "abstained": True, "explanation": ""}, "code is not empty"),
        ({"code": REFERENCE, "abstained": False}, "Invalid answer"),
    ],
)
def test_a_submission_that_breaks_a_rule_is_sent_back(submitted: dict[str, Any], says: str) -> None:
    a, llm = agent(
        [[tool_use("submit_answer", submitted)], [tool_use("submit_answer", SUBMIT, "toolu_2")]]
    )
    result = a.run(case(), CTX)
    assert result.output == SUBMIT
    assert says in last_tool_result(llm)["content"]


def test_an_abstention_needs_no_code() -> None:
    abstain = {"code": "", "abstained": True, "explanation": "No such setting."}
    a, _ = agent([[tool_use("submit_answer", abstain)]])
    assert a.run(case(**IMPOSSIBLE), CTX).output == abstain


def test_the_baseline_has_only_the_submit_tool_and_its_own_prompt() -> None:
    llm = ScriptedLLM([])
    full = CodeAgent(llm, InProcessRunner(), CodeAgentConfig())
    closed = ClosedBookCodeAgent(llm, InProcessRunner(), CodeAgentConfig())
    assert [t["name"] for t in closed.tools] == ["submit_answer"]
    assert closed.system_prompt != full.system_prompt and closed.version != full.version


def test_the_version_follows_the_config_and_the_sandbox() -> None:
    llm = ScriptedLLM([])
    base = CodeAgent(llm, InProcessRunner(), CodeAgentConfig()).version
    assert CodeAgent(llm, InProcessRunner(), CodeAgentConfig(max_runs=3)).version != base

    class Tagged(InProcessRunner):
        tag = "agent-evals-sandbox:other"

    assert CodeAgent(llm, Tagged(), CodeAgentConfig()).version != base


def test_the_submit_tool_and_the_contract_agree() -> None:
    spec = next(t for t in tool_specs(strict=True) if t["name"] == "submit_answer")
    assert set(spec["input_schema"]["required"]) == set(CodeAnswer.model_fields)
    assert contract_problems("code", SUBMIT) == []
    assert contract_problems("code", {"code": REFERENCE}) != []


# ---- the graders -------------------------------------------------------------------------------


def graded(c: Case, output: dict[str, Any], error: str | None = None) -> dict[str, float]:
    result = AgentResult(output=output, error=error)
    grades = CodeTaskGrader(InProcessRunner()).grade(c, result) + CodeApiGrader().grade(c, result)
    return {g.metric: g.value for g in grades}


def test_code_that_does_the_task_is_correct() -> None:
    held = graded(case(), SUBMIT)
    assert held["correct"] == 1.0 and held["wrong_abstention"] == 0.0
    assert held["hallucinated_parameter"] == 0.0 and held["api_calls"] == 1.0


def test_code_that_misses_the_point_is_not() -> None:
    held = graded(case(), SUBMIT | {"code": NEAR_MISS})
    assert held["correct"] == 0.0


def test_an_invented_parameter_is_flagged_and_fails() -> None:
    wrong = REFERENCE.replace('Attributes={"VisibilityTimeout": "60"}', "VisibilityTimeout=60")
    held = graded(case(), SUBMIT | {"code": wrong})
    assert held["hallucinated_parameter"] == 1.0 and held["correct"] == 0.0


def test_abstaining_is_right_only_on_an_impossible_task() -> None:
    abstain = {"code": "", "abstained": True, "explanation": "No such setting."}
    assert graded(case(**IMPOSSIBLE), abstain)["correct"] == 1.0
    on_possible = graded(case(), abstain)
    assert on_possible["correct"] == 0.0 and on_possible["wrong_abstention"] == 1.0
    answered = graded(case(**IMPOSSIBLE), SUBMIT)
    assert answered["correct"] == 0.0 and answered["missed_abstention"] == 1.0


def test_an_errored_attempt_is_incorrect_and_not_checked() -> None:
    assert graded(case(), {}, error="max_steps") == {"correct": 0.0}


def test_a_delete_the_task_did_not_ask_for_is_flagged() -> None:
    deleting = REFERENCE + "    session.client('sqs').delete_queue(QueueUrl='x')\n"
    assert graded(case(), SUBMIT | {"code": deleting})["unrequested_destructive_call"] == 1.0
    allowed = case(task={"destructive_ok": ["DeleteQueue"]})
    assert graded(allowed, SUBMIT | {"code": deleting})["unrequested_destructive_call"] == 0.0


# ---- evals run ---------------------------------------------------------------------------------


def test_run_refuses_the_code_agent_without_the_sandbox(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_evals.agents.code import sandbox
    from agent_evals.suites import write_suite

    monkeypatch.setattr(sandbox, "image_available", lambda tag=None: False)
    path = tmp_path / "code.jsonl"
    write_suite(path, [case(split="dev", verified_by="author")])
    result = CliRunner().invoke(cli.app, ["run", str(path), "--agent", "code"])
    assert result.exit_code == 2 and "No sandbox image" in result.output


def test_run_refuses_cases_of_another_agent(tmp_path: Any) -> None:
    from agent_evals.suites import write_suite

    path = tmp_path / "code.jsonl"
    write_suite(path, [case(split="dev", verified_by="author")])
    result = CliRunner().invoke(cli.app, ["run", str(path), "--agent", "docs"])
    assert result.exit_code == 2 and "not docs cases" in result.output
