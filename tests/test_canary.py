"""The canary rule and the automatic rollback (docs/plans/phase-8-llm-ops.md, part 6), offline."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPT_V3, SYSTEM_PROMPTS
from agent_evals.config import Config, DocsAgentConfig, Pricing, ServiceConfig
from agent_evals.corpus import Corpus
from agent_evals.harness.llm import LLMResponse
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.canary import ALPHA, LOOKS, PER_LOOK, CanaryWatch, fires, is_bad
from agent_evals.service.drills import (
    DRILL_FAULT,
    DRILL_HARMLESS,
    DRILL_PROMPTS,
    DRILL_STALE_NOTE,
    FAULT_RULE,
    STALE_NOTE,
)
from agent_evals.service.fake import FakeProvider
from agent_evals.service.main import prompts_of, registry_of
from agent_evals.service.registry import (
    CanaryGone,
    PromptRegistry,
    RegistryError,
)
from agent_evals.stats.monitor import fisher_greater
from agent_evals.types import AgentResult, Case, RunContext, Usage
from conftest import BUCKETS_URL

PROMPTS = {"v1": "first prompt", "v2": "second prompt", "v3": "third prompt"}
GOOD = {
    "answer": "Between 3 and 63 characters.",
    "citations": [{"url": BUCKETS_URL, "quote": "between 3 and 63 characters long"}],
    "abstained": False,
}
ABSTAINED = {"answer": "Not in the docs.", "citations": [], "abstained": True}
UNCITED = {"answer": "It is 63.", "citations": [], "abstained": False}


class Agent:
    """Answers by its prompt version, or by what a test gives it."""

    name = "docs"

    def __init__(self, version: str, run: Callable[[str], AgentResult]) -> None:
        self.version = f"agent-{version}"
        self.prompt_version = version
        self.answer = run

    def describe(self) -> dict[str, Any]:
        return {}

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        return self.answer(self.prompt_version)


def answered(output: dict[str, Any]) -> AgentResult:
    return AgentResult(
        output=output, usage=Usage(input_tokens=100, output_tokens=20, model_calls=1)
    )


def registry(tmp_path: Path) -> PromptRegistry:
    return PromptRegistry.create(tmp_path / "registry.yaml", tmp_path / "log.jsonl", PROMPTS, "v1")


def service(
    tmp_path: Path,
    prompts: PromptRegistry,
    run: Callable[[str], AgentResult],
    budget: float = 1.0,
) -> tuple[TestClient, Deps]:
    deps = Deps(
        settings=ServiceConfig(max_concurrent=4),
        registry=prompts,
        agent_for=lambda version: Agent(version, run),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=5.0),
        budget=DailyBudget(tmp_path / "spend.json", budget),
    )
    return TestClient(create_app(deps)), deps


def ask(c: TestClient) -> dict[str, Any]:
    response = c.post("/v1/ask", json={"question": "How long can a bucket name be?"})
    held: dict[str, Any] = response.json()
    return held


def only_the_canary_abstains(version: str) -> AgentResult:
    return answered(ABSTAINED if version == "v2" else GOOD)


def counted(watch: CanaryWatch, *answers: tuple[str, bool, int]) -> list[Any]:
    """Feed (role, bad, how many) in turn, and return the looks that came of it."""
    looks = []
    for role, bad, times in answers:
        for _ in range(times):
            look = watch.record("run", role, bad)  # type: ignore[arg-type]
            if look is not None:
                looks.append(look)
    return looks


def watching() -> CanaryWatch:
    watch = CanaryWatch()
    watch.follow("run")
    return watch


# ---- the signal --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("errored", "abstained", "citations", "bad"),
    [
        (False, False, 2, False),
        (True, False, 2, True),
        (False, True, 2, True),
        (False, False, 0, True),
    ],
)
def test_a_bad_answer_is_an_error_an_abstention_or_no_citation(
    errored: bool, abstained: bool, citations: int, bad: bool
) -> None:
    assert is_bad(errored=errored, abstained=abstained, citations=citations) is bad


# ---- the looks ---------------------------------------------------------------------------------


def test_the_rule_is_as_agreed() -> None:
    assert LOOKS == (20, 40, 60)
    assert ALPHA == 0.01
    assert pytest.approx(0.01 / 3) == PER_LOOK


def test_each_look_is_held_to_a_third_of_the_overall_rate() -> None:
    # 7 of 20 against 7 of 80 clears the overall 1%, but not the 0.33% each look is held to.
    assert PER_LOOK < fisher_greater(7, 20, 7, 80) < ALPHA
    assert fires(7, 20, 7, 80)[1] is False
    assert fires(8, 20, 7, 80)[1] is True


def test_the_rule_fires_at_the_first_look_on_a_badly_broken_canary() -> None:
    watch = watching()
    looks = counted(watch, ("champion", False, 80), ("canary", True, 20))
    assert [(look.number, look.fired) for look in looks] == [(1, True)]
    assert watch.stopped


def test_looks_come_only_at_20_40_and_60_then_a_person_decides() -> None:
    watch = watching()
    looks = counted(watch, ("champion", False, 240), ("canary", False, 100))
    assert [look.canary_answers for look in looks] == list(LOOKS)
    assert [look.fired for look in looks] == [False, False, False]
    assert looks[-1].last
    assert not watch.stopped
    assert counted(watch, ("canary", True, 100)) == []  # decided: nothing more counts


def test_a_look_compares_the_canary_with_the_champion_over_the_same_period() -> None:
    watch = watching()
    # Half the champion's answers are bad too, as in an outage, so a bad canary is not news.
    looks = counted(
        watch,
        ("champion", True, 40),
        ("champion", False, 40),
        ("canary", True, 10),
        ("canary", False, 10),
    )
    assert (looks[0].canary_bad, looks[0].champion_bad, looks[0].champion_answers) == (10, 40, 80)
    assert not looks[0].fired


def test_a_look_says_why_in_numbers() -> None:
    look = counted(watching(), ("champion", False, 80), ("canary", True, 20))[0]
    assert "look 1: 20 of 20 canary answers bad, 0 of 80 on the champion" in look.why()


def test_an_answer_from_another_run_does_not_count() -> None:
    watch = watching()
    assert watch.record("old-run", "canary", True) is None
    assert watch.record(None, "canary", True) is None
    assert counted(watch, ("canary", False, 19)) == []  # still needs its own 20


def test_a_new_run_starts_with_fresh_counts() -> None:
    watch = watching()
    counted(watch, ("champion", False, 80), ("canary", True, 20))
    assert watch.stopped
    watch.follow("next-run")
    assert not watch.stopped
    assert watch.record("next-run", "canary", True) is None


def test_the_simulation_runs_the_services_own_rule() -> None:
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "canary_power.py"
    spec = importlib.util.spec_from_file_location("canary_power", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["canary_power"] = module
    spec.loader.exec_module(module)
    assert module.fires is fires
    assert module.LOOKS is LOOKS


# ---- the registry ------------------------------------------------------------------------------


def test_each_release_of_a_canary_is_a_new_run(tmp_path: Path) -> None:
    held = registry(tmp_path)
    first = held.set_canary("v2", 0.2, who="me", why="try").canary_run
    held.rollback(who="me", why="stop")
    second = held.set_canary("v2", 0.2, who="me", why="again").canary_run
    assert first and second and first != second


def test_a_file_with_a_canary_and_no_run_is_refused(tmp_path: Path) -> None:
    held = registry(tmp_path)
    state = held.set_canary("v2", 0.2, who="me", why="try")
    text = held.path.read_text(encoding="utf-8")
    held.path.write_text(
        text.replace(f"canary_run: {state.canary_run}", "canary_run: null"), "utf-8"
    )
    with pytest.raises(RegistryError, match="cannot be read"):
        PromptRegistry(held.path, held.log_path, PROMPTS)


def test_dropping_a_canary_changes_nothing_else_and_says_who_did_it(tmp_path: Path) -> None:
    held = registry(tmp_path)
    run = held.set_canary("v2", 0.2, who="me", why="try").canary_run
    assert run is not None
    after = held.drop_canary(run, who="canary-rule", why="look 1")
    assert after.champion.version == "v1"
    assert (after.canary, after.canary_share, after.canary_run) == (None, 0.0, None)
    assert '"action": "auto-rollback"' in held.log_path.read_text(encoding="utf-8").splitlines()[-1]


def test_the_automatic_rollback_never_moves_the_champion(tmp_path: Path) -> None:
    """A plain rollback with no canary goes back a champion. If a person drops the canary just
    before the rule acts, the rule must not then undo the promoted champion."""
    held = registry(tmp_path)
    held.set_canary("v2", 0.2, who="me", why="try")
    held.promote(who="me", why="good")
    run = held.set_canary("v3", 0.2, who="me", why="next").canary_run
    assert run is not None
    held.rollback(who="a person", why="looks bad")
    with pytest.raises(CanaryGone):
        held.drop_canary(run, who="canary-rule", why="look 1")
    assert held.state().champion.version == "v2"


# ---- the service -------------------------------------------------------------------------------


def run_until_rolled_back(c: TestClient, held: PromptRegistry, most: int = 400) -> list[Any]:
    served = []
    for _ in range(most):
        served.append(ask(c))
        if held.state().canary is None:
            return served
    raise AssertionError("the rule never fired")


def test_a_broken_canary_rolls_itself_back_and_traffic_returns_to_the_champion(
    tmp_path: Path,
) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, _ = service(tmp_path, held, only_the_canary_abstains)
    served = run_until_rolled_back(c, held)
    assert held.state().champion.version == "v1"
    assert sum(answer["role"] == "canary" for answer in served) == LOOKS[0]
    assert {ask(c)["role"] for _ in range(30)} == {"champion"}
    log = held.log_path.read_text(encoding="utf-8").splitlines()[-1]
    assert '"who": "canary-rule"' in log and "20 of 20 canary answers bad" in log
    metrics = c.get("/metrics").text
    assert "canary_rollbacks_total 1.0" in metrics
    assert 'canary_answers_total{grade="bad",role="canary"} 20.0' in metrics


def test_an_answer_with_no_citation_is_bad_for_the_rule(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, _ = service(tmp_path, held, lambda v: answered(UNCITED if v == "v2" else GOOD))
    run_until_rolled_back(c, held)
    assert held.state().canary is None


def test_a_failed_answer_is_bad_for_the_rule(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")

    def run(version: str) -> AgentResult:
        if version == "v2":
            raise RuntimeError("the provider failed")
        return answered(GOOD)

    c, _ = service(tmp_path, held, run)
    run_until_rolled_back(c, held)
    assert "canary_rollbacks_total 1.0" in c.get("/metrics").text


def test_a_produced_nothing_answer_is_bad_for_the_rule(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")

    def run(version: str) -> AgentResult:
        if version == "v2":
            return AgentResult(output={}, error="max_steps")
        return answered(GOOD)

    c, _ = service(tmp_path, held, run)
    run_until_rolled_back(c, held)
    assert held.state().canary is None


def test_a_canary_that_is_no_worse_stays_for_a_person(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, _ = service(tmp_path, held, lambda _: answered(GOOD))
    for _ in range(170):
        ask(c)
    metrics = c.get("/metrics").text
    assert held.state().canary is not None
    assert "canary_passed_total 1.0" in metrics
    assert "canary_rollbacks_total 0.0" in metrics


def test_a_fired_rule_stops_the_canary_even_if_the_file_cannot_be_changed(tmp_path: Path) -> None:
    class Stuck(PromptRegistry):
        def drop_canary(self, run: str, *, who: str, why: str) -> Any:
            raise RegistryError("the file cannot be written")

    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    stuck = Stuck(held.path, held.log_path, PROMPTS)
    c, _ = service(tmp_path, stuck, only_the_canary_abstains)
    while "canary_rollbacks_total 1.0" not in c.get("/metrics").text:
        ask(c)
    assert stuck.state().canary is not None  # the file still names it
    assert {ask(c)["role"] for _ in range(30)} == {"champion"}
    assert "canary_rollback_failures_total 1.0" in c.get("/metrics").text


def test_a_person_getting_there_first_is_not_a_failure(tmp_path: Path) -> None:
    class Beaten(PromptRegistry):
        def drop_canary(self, run: str, *, who: str, why: str) -> Any:
            raise CanaryGone("gone")

    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, _ = service(tmp_path, Beaten(held.path, held.log_path, PROMPTS), only_the_canary_abstains)
    while "canary_rollbacks_total 1.0" not in c.get("/metrics").text:
        ask(c)
    assert "canary_rollback_failures_total 0.0" in c.get("/metrics").text


def test_a_refused_request_is_not_an_answer(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, _ = service(tmp_path, held, only_the_canary_abstains, budget=0.0)
    assert c.post("/v1/ask", json={"question": "Any?"}).status_code == 503
    assert "canary_answers_total{" not in c.get("/metrics").text


def test_no_canary_means_nothing_is_counted(tmp_path: Path) -> None:
    c, _ = service(tmp_path, registry(tmp_path), lambda _: answered(ABSTAINED))
    ask(c)
    assert "canary_answers_total{" not in c.get("/metrics").text


def test_a_rollback_by_a_person_mid_canary_ends_the_watch(tmp_path: Path) -> None:
    held = registry(tmp_path)
    held.set_canary("v2", 0.5, who="me", why="try")
    c, deps = service(tmp_path, held, lambda _: answered(GOOD))
    ask(c)
    held.rollback(who="a person", why="changed my mind")
    ask(c)
    assert deps.canary.record("anything", "canary", True) is None


# ---- the drill prompts -------------------------------------------------------------------------


def test_each_drill_prompt_is_the_champion_with_one_change() -> None:
    for text in DRILL_PROMPTS.values():
        assert text != SYSTEM_PROMPT_V3
    assert FAULT_RULE in DRILL_PROMPTS[DRILL_FAULT]
    assert "How to work" not in DRILL_PROMPTS[DRILL_HARMLESS]
    assert not set(DRILL_PROMPTS) & set(SYSTEM_PROMPTS)


def test_the_stale_note_drill_is_the_champion_with_the_notice_added() -> None:
    assert DRILL_PROMPTS[DRILL_STALE_NOTE] == SYSTEM_PROMPT_V3 + STALE_NOTE


def test_a_production_service_refuses_a_drill_prompt(tmp_path: Path) -> None:
    production = Config(
        service=ServiceConfig(
            registry_path=tmp_path / "registry.yaml", registry_log=tmp_path / "log.jsonl"
        )
    )
    assert DRILL_FAULT not in prompts_of(production)
    PromptRegistry.create(
        production.service.registry_path,
        production.service.registry_log,
        prompts_of(production),
        "v3",
    )
    with pytest.raises(RegistryError, match="unknown prompt version"):
        registry_of(production).set_canary(DRILL_FAULT, 0.2, who="me", why="try")


def test_a_drill_service_serves_a_drill_prompt_but_the_config_must_say_so(tmp_path: Path) -> None:
    drill = Config(
        service=ServiceConfig(
            drill=True,
            registry_path=tmp_path / "registry.yaml",
            registry_log=tmp_path / "log.jsonl",
        )
    )
    PromptRegistry.create(
        drill.service.registry_path, drill.service.registry_log, prompts_of(drill), "v3"
    )
    state = registry_of(drill).set_canary(DRILL_FAULT, 0.2, who="me", why="drill")
    assert state.canary is not None and state.canary.version == DRILL_FAULT


def test_the_agent_takes_a_drill_prompt_only_when_handed_it() -> None:
    settings = DocsAgentConfig(prompt_version=DRILL_FAULT)
    with pytest.raises(ValueError, match=r"unknown docs_agent.prompt_version"):
        DocsAgent(FakeProvider(0.0), Corpus([]), settings)
    agent = DocsAgent(FakeProvider(0.0), Corpus([]), settings, prompts=DRILL_PROMPTS)
    assert agent.system_prompt == DRILL_PROMPTS[DRILL_FAULT]


def test_handing_the_agent_prompts_does_not_change_a_gated_version() -> None:
    settings = DocsAgentConfig(prompt_version="v3")
    plain = DocsAgent(FakeProvider(0.0), Corpus([]), settings)
    handed = DocsAgent(
        FakeProvider(0.0), Corpus([]), settings, prompts={**SYSTEM_PROMPTS, **DRILL_PROMPTS}
    )
    assert plain.version == handed.version


def test_the_fake_provider_follows_the_fault_rule_so_a_drill_can_be_rehearsed_for_free() -> None:
    fake = FakeProvider(0.0)
    request = {
        "system": DRILL_PROMPTS[DRILL_FAULT],
        "messages": [{"role": "user", "content": "How long can a bucket name be?"}],
    }
    response: LLMResponse = fake.create(request, salt="s")
    assert response.content[0]["input"]["abstained"] is True
    request["system"] = DRILL_PROMPTS[DRILL_HARMLESS]
    assert fake.create(request, salt="s").content[0]["name"] == "search_docs"
