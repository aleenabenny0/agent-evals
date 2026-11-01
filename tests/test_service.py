"""The service (docs/plans/phase-8-llm-ops.md, parts 1 and 2), offline: a scripted agent, no API."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.config import Pricing, ServiceConfig, load_config
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.registry import (
    Pointer,
    PromptRegistry,
    RegistryError,
    RegistryState,
    assign,
    prompt_hash,
)
from agent_evals.types import AgentResult, Case, RunContext, Step, Usage
from conftest import BUCKETS_URL

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = {"v1": "first prompt", "v2": "second prompt", "v3": "third prompt"}
PRICING = Pricing(input_per_mtok=1.0, output_per_mtok=5.0)
ANSWER = {
    "answer": "Between 3 and 63 characters.",
    "citations": [{"url": BUCKETS_URL, "quote": "between 3 and 63 characters long"}],
    "abstained": False,
}


class FakeAgent:
    name = "docs"

    def __init__(self, version: str, run: Callable[[Case], AgentResult] | None = None) -> None:
        self.prompt_version = version
        self.behavior = run
        self.seen: list[Case] = []

    @property
    def version(self) -> str:
        return f"agent-{self.prompt_version}"

    def describe(self) -> dict[str, Any]:
        return {}

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        self.seen.append(case)
        if self.behavior is not None:
            return self.behavior(case)
        return AgentResult(
            output=ANSWER, usage=Usage(input_tokens=1000, output_tokens=200, model_calls=2)
        )


def registry(tmp_path: Path, version: str = "v1") -> PromptRegistry:
    return PromptRegistry.create(
        tmp_path / "registry.yaml", tmp_path / "log.jsonl", PROMPTS, version
    )


def client(
    tmp_path: Path,
    *,
    run: Callable[[Case], AgentResult] | None = None,
    prompts: PromptRegistry | None = None,
    **settings: Any,
) -> tuple[TestClient, Deps]:
    deps = Deps(
        settings=ServiceConfig(**settings),
        registry=prompts or registry(tmp_path),
        agent_for=lambda version: FakeAgent(version, run),
        pricing=PRICING,
        budget=DailyBudget(tmp_path / "spend.json", 1.0),
    )
    return TestClient(create_app(deps)), deps


def metric(c: TestClient, line: str) -> bool:
    return line in c.get("/metrics").text


# ---- the registry ------------------------------------------------------------------------------


def test_the_registry_serves_the_champion_and_a_canary_by_a_hash_of_the_request_id(
    tmp_path: Path,
) -> None:
    held = registry(tmp_path)
    assert assign(held.state(), "any").version == "v1"
    state = held.set_canary("v2", 0.25, who="me", why="try it")
    drawn = [assign(state, f"request-{n}") for n in range(4000)]
    share = sum(a.role == "canary" for a in drawn) / len(drawn)
    assert 0.22 < share < 0.28
    assert {a.version for a in drawn} == {"v1", "v2"}
    assert assign(state, "request-7") == assign(state, "request-7")  # deterministic


def test_promote_and_rollback_move_the_pointers_and_log_who_why_from_and_to(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try it")
    state = held.promote(who="me", why="it held")
    assert state.champion.version == "v2" and state.canary is None and state.canary_share == 0
    assert state.previous is not None and state.previous.version == "v1"
    state = held.rollback(who="pager", why="errors")
    assert state.champion.version == "v1" and state.previous is None
    with pytest.raises(RegistryError, match="nothing to roll back"):
        held.rollback(who="pager", why="again")
    log = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text("utf-8").splitlines()]
    assert [e["action"] for e in log] == ["canary", "promote", "rollback"]
    assert log[2]["who"] == "pager" and log[2]["why"] == "errors"
    assert log[2]["from"]["champion"]["version"] == "v2"
    assert log[2]["to"]["champion"]["version"] == "v1"


def test_a_rollback_with_a_canary_drops_the_canary_and_leaves_the_champion(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try it")
    state = held.rollback(who="rule", why="canary failing")
    assert state.champion.version == "v1" and state.canary is None and state.canary_share == 0


def test_the_registry_refuses_what_it_cannot_stand_behind(tmp_path: Path) -> None:
    held = registry(tmp_path)
    with pytest.raises(RegistryError, match="unknown prompt version"):
        held.set_canary("v9", 0.2, who="me", why="x")
    with pytest.raises(RegistryError, match="champion already"):
        held.set_canary("v1", 0.2, who="me", why="x")
    with pytest.raises(RegistryError, match="above 0 and at most 1"):
        held.set_canary("v2", 0.0, who="me", why="x")
    with pytest.raises(RegistryError, match="no canary to promote"):
        held.promote(who="me", why="x")
    with pytest.raises(RegistryError, match="needs a reason"):
        held.set_canary("v2", 0.2, who="me", why="  ")
    with pytest.raises(RegistryError, match="exists"):
        registry(tmp_path)
    assert not (tmp_path / "log.jsonl").exists()


def test_a_prompt_text_cannot_change_under_a_registered_version(tmp_path: Path) -> None:
    registry(tmp_path)
    edited = {**PROMPTS, "v1": "first prompt, quietly edited"}
    with pytest.raises(RegistryError, match="not the text that was registered"):
        PromptRegistry(tmp_path / "registry.yaml", tmp_path / "log.jsonl", edited)


def test_another_process_changing_the_file_takes_effect_with_no_restart(tmp_path: Path) -> None:
    serving = registry(tmp_path)
    assert serving.state().canary is None
    operator = PromptRegistry(tmp_path / "registry.yaml", tmp_path / "log.jsonl", PROMPTS)
    operator.set_canary("v2", 0.5, who="me", why="try it")
    state = serving.state()
    assert state.canary is not None and state.canary.version == "v2"


def test_a_bad_file_leaves_the_last_good_state_in_force_and_says_so(tmp_path: Path) -> None:
    held = registry(tmp_path)
    (tmp_path / "registry.yaml").write_text("champion: [broken", encoding="utf-8")
    assert held.state().champion.version == "v1"
    assert held.error is not None and "cannot be read" in held.error
    state = RegistryState(champion=Pointer(version="v2", sha256=prompt_hash(PROMPTS["v2"])))
    (tmp_path / "registry.yaml").write_text(
        json.dumps(state.model_dump(mode="json")), encoding="utf-8"
    )
    assert held.state().champion.version == "v2" and held.error is None


def test_the_committed_registry_names_the_champion_and_its_text() -> None:
    config = load_config(ROOT / "configs" / "service.yaml")
    log = ROOT / "results" / "unused-log.jsonl"
    held = PromptRegistry(ROOT / config.service.registry_path, log, SYSTEM_PROMPTS)
    default = load_config(ROOT / "configs" / "default.yaml")
    assert held.state().champion.version == default.docs_agent.prompt_version
    assert held.state().canary is None and not log.exists()
    assert config.service.host == "127.0.0.1"
    assert config.docs_agent.model == default.docs_agent.model


# ---- answering ---------------------------------------------------------------------------------


def test_a_question_gets_a_cited_answer_with_its_version_and_cost(tmp_path: Path) -> None:
    c, _ = client(tmp_path)
    r = c.post("/v1/ask", json={"question": "How long can a bucket name be?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == ANSWER["answer"] and body["citations"] == ANSWER["citations"]
    assert body["abstained"] is False
    assert (body["prompt_version"], body["role"], body["agent_version"]) == (
        "v1",
        "champion",
        "agent-v1",
    )
    assert body["cost_usd"] == pytest.approx((1000 * 1 + 200 * 5) / 1e6)
    assert body["model_calls"] == 2 and len(body["request_id"]) == 32
    assert body["providers"] == []  # no gateway behind this agent, so nothing is reported


def test_each_request_is_its_own_sample_with_a_case_the_agent_accepts(tmp_path: Path) -> None:
    seen: list[Case] = []

    def run(case: Case) -> AgentResult:
        seen.append(case)
        return AgentResult(output=ANSWER)

    c, _ = client(tmp_path, run=run)
    for _ in range(2):
        c.post("/v1/ask", json={"question": "same question"})
    assert seen[0].id != seen[1].id and seen[0].gold is None and seen[0].split == "online"


def test_a_canary_serves_its_share_and_a_rollback_stops_it_with_no_restart(tmp_path: Path) -> None:
    held = registry(tmp_path)
    c, _ = client(tmp_path, prompts=held)
    held.set_canary("v2", 1.0, who="me", why="try it")
    assert c.post("/v1/ask", json={"question": "q"}).json()["prompt_version"] == "v2"
    assert c.post("/v1/ask", json={"question": "q"}).json()["role"] == "canary"
    held.rollback(who="me", why="bad")
    body = c.post("/v1/ask", json={"question": "q"}).json()
    assert (body["prompt_version"], body["role"]) == ("v1", "champion")


def test_an_abstention_is_an_answer_and_is_counted_apart(tmp_path: Path) -> None:
    c, _ = client(
        tmp_path,
        run=lambda _: AgentResult(output={"answer": "", "abstained": True, "citations": []}),
    )
    body = c.post("/v1/ask", json={"question": "q"}).json()
    assert body["abstained"] is True and body["citations"] == []
    assert metric(
        c, 'ask_requests_total{outcome="abstained",prompt_version="v1",role="champion"} 1.0'
    )


# ---- limits ------------------------------------------------------------------------------------


def test_a_question_too_long_or_empty_or_malformed_is_refused_without_the_agent(
    tmp_path: Path,
) -> None:
    calls: list[Case] = []

    def run(case: Case) -> AgentResult:
        calls.append(case)
        return AgentResult(output=ANSWER)

    c, _ = client(tmp_path, run=run, max_question_chars=10)
    assert c.post("/v1/ask", json={"question": "x" * 11}).status_code == 422
    assert c.post("/v1/ask", json={"question": ""}).status_code == 422
    assert c.post("/v1/ask", json={"wrong": "x"}).json()["error"] == "invalid"
    assert calls == []


def test_past_capacity_a_request_is_refused_at_once_and_a_slot_comes_back(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def run(case: Case) -> AgentResult:
        entered.set()
        release.wait(5)
        return AgentResult(output=ANSWER)

    c, _ = client(tmp_path, run=run, max_concurrent=1, retry_after_s=7)
    first: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: first.update(r=c.post("/v1/ask", json={"question": "q"}))
    )
    worker.start()
    assert entered.wait(5)
    refused = c.post("/v1/ask", json={"question": "q"})
    assert refused.status_code == 429 and refused.headers["retry-after"] == "7"
    assert refused.json()["error"] == "overloaded"
    assert metric(c, "ask_in_flight 1.0") and metric(c, "ask_capacity 1.0")
    release.set()
    worker.join(5)
    assert first["r"].status_code == 200
    assert c.post("/v1/ask", json={"question": "q"}).status_code == 200


def test_a_timed_out_answer_keeps_its_slot_until_it_ends_and_its_spend_counts(
    tmp_path: Path,
) -> None:
    release, ended = threading.Event(), threading.Event()

    def run(case: Case) -> AgentResult:
        release.wait(5)
        return AgentResult(output=ANSWER, usage=Usage(input_tokens=1000, output_tokens=0))

    c, deps = client(tmp_path, run=run, max_concurrent=1, answer_timeout_s=0.05)
    r = c.post("/v1/ask", json={"question": "q"})
    assert r.status_code == 504 and r.json()["error"] == "timeout"
    assert c.post("/v1/ask", json={"question": "q"}).status_code == 429  # still working on it
    release.set()
    for _ in range(200):
        if deps.budget.spent_usd() > 0:
            ended.set()
            break
        threading.Event().wait(0.01)
    assert ended.is_set() and deps.budget.spent_usd() == pytest.approx(0.001)


def test_an_api_failure_and_an_agent_failure_are_different_errors(tmp_path: Path) -> None:
    def boom(case: Case) -> AgentResult:
        raise RuntimeError("the API is down")

    c, _ = client(tmp_path, run=boom)
    r = c.post("/v1/ask", json={"question": "q"})
    assert r.status_code == 502 and r.json()["error"] == "provider_error"
    assert "the API is down" not in r.text
    c, _ = client(tmp_path / "b", run=lambda _: AgentResult(error="malformed_submission"))
    r = c.post("/v1/ask", json={"question": "q"})
    assert r.status_code == 502 and r.json()["error"] == "agent_failed"
    assert c.post("/v1/ask", json={"question": "q"}).status_code == 502  # the slot came back


def test_the_daily_budget_refuses_past_the_cap_survives_a_restart_and_resets_tomorrow(
    tmp_path: Path,
) -> None:
    days = [date(2026, 11, 1)]
    budget = DailyBudget(tmp_path / "spend.json", 0.01, today=lambda: days[0])
    budget.add(0.004)
    assert not budget.exhausted()
    budget.add(0.006)
    assert budget.exhausted()
    again = DailyBudget(tmp_path / "spend.json", 0.01, today=lambda: days[0])
    assert again.exhausted() and again.spent_usd() == pytest.approx(0.01)
    days[0] = date(2026, 11, 2)
    assert not again.exhausted() and again.spent_usd() == 0.0


def test_over_budget_the_service_refuses_and_says_it_is_not_ready(tmp_path: Path) -> None:
    calls: list[Case] = []

    def run(case: Case) -> AgentResult:
        calls.append(case)
        return AgentResult(output=ANSWER)

    c, deps = client(tmp_path, run=run)
    deps.budget.add(5.0)
    r = c.post("/v1/ask", json={"question": "q"})
    assert r.status_code == 503 and r.json()["error"] == "over_budget" and calls == []
    assert c.get("/readyz").status_code == 503
    assert c.get("/healthz").status_code == 200  # up, though not ready
    assert metric(
        c, 'ask_requests_total{outcome="over_budget",prompt_version="none",role="none"} 1.0'
    )


# ---- health, metrics and privacy -------------------------------------------------------------


def test_ready_reads_every_check_and_a_bad_registry_file(tmp_path: Path) -> None:
    held = registry(tmp_path)
    up = {"on": True}
    deps = Deps(
        settings=ServiceConfig(),
        registry=held,
        agent_for=lambda v: FakeAgent(v),
        pricing=PRICING,
        budget=DailyBudget(tmp_path / "spend.json", 1.0),
        ready_checks={"snapshot": lambda: up["on"]},
    )
    c = TestClient(create_app(deps))
    assert c.get("/readyz").json() == {
        "ready": True,
        "checks": {"snapshot": True, "registry_file": True, "budget_left": True},
    }
    up["on"] = False
    assert c.get("/readyz").status_code == 503
    up["on"] = True
    (tmp_path / "registry.yaml").write_text("not: [valid", encoding="utf-8")
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["registry_file"] is False
    assert c.post("/v1/ask", json={"question": "q"}).status_code == 200  # the last good state
    assert metric(c, "registry_file_bad 1.0")


def test_metrics_count_tokens_cost_calls_and_tool_errors_by_version(tmp_path: Path) -> None:
    trajectory = [
        Step(index=0, kind="tool", name="read_page", is_error=True),
        Step(index=1, kind="model", name="m", is_error=True),
    ]

    def run(case: Case) -> AgentResult:
        usage = Usage(input_tokens=1000, output_tokens=200, model_calls=2)
        return AgentResult(output=ANSWER, trajectory=trajectory, usage=usage)

    c, _ = client(tmp_path, run=run)
    c.post("/v1/ask", json={"question": "q"})
    text = c.get("/metrics").text
    assert 'llm_tokens_total{kind="input",prompt_version="v1"} 1000.0' in text
    assert 'llm_tokens_total{kind="output",prompt_version="v1"} 200.0' in text
    assert 'llm_model_calls_total{prompt_version="v1"} 2.0' in text
    assert 'llm_cost_usd_total{prompt_version="v1"} 0.002' in text
    assert 'agent_tool_errors_total{prompt_version="v1"} 1.0' in text  # the model step is no tool
    assert 'ask_latency_seconds_count{prompt_version="v1",role="champion"} 1.0' in text


def test_no_question_text_reaches_a_log_or_a_metric(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "my account 123456789012 has a private bucket"
    caplog.set_level(logging.INFO, logger="agent_evals.service")
    c, _ = client(tmp_path)
    c.post("/v1/ask", json={"question": secret})
    assert '"question_chars": 44' in caplog.text and "question_sha256" in caplog.text
    assert "123456789012" not in caplog.text and "123456789012" not in c.get("/metrics").text


# ---- the commands ------------------------------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "service.yaml"
    path.write_text(
        "service:\n"
        f'  registry_path: "{(tmp_path / "registry.yaml").as_posix()}"\n'
        f'  registry_log: "{(tmp_path / "log.jsonl").as_posix()}"\n',
        encoding="utf-8",
    )
    PromptRegistry.create(tmp_path / "registry.yaml", tmp_path / "log.jsonl", SYSTEM_PROMPTS, "v3")
    return path


def test_the_registry_commands_change_the_pointers_and_refuse_with_exit_2(tmp_path: Path) -> None:
    config = str(_config(tmp_path))
    run = CliRunner().invoke
    shown = run(cli.app, ["registry", "show", "-c", config])
    assert shown.exit_code == 0 and "champion v3, canary none" in shown.output
    made = run(
        cli.app, ["registry", "canary", "v4", "--why", "try it", "--share", "0.1", "-c", config]
    )
    assert made.exit_code == 0 and "canary v4 at 10%" in made.output
    back = run(cli.app, ["registry", "rollback", "--why", "bad", "--who", "pager", "-c", config])
    assert back.exit_code == 0 and "canary none" in back.output
    bad = run(cli.app, ["registry", "promote", "--why", "x", "-c", config])
    assert bad.exit_code == 2 and "no canary to promote" in bad.output
    log = (tmp_path / "log.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(x)["who"] for x in log] == ["operator", "pager"]
