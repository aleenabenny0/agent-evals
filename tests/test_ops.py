"""Traces, the fake provider, gate receipts and the deployment files (phase 8, parts 4, 5 and 7),
offline."""

from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_evals.agents.docs import agent as agent_module
from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.config import DocsAgentConfig, Pricing, ServiceConfig, load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.gateway import CircuitBreaker, Provider, ResilientClient
from agent_evals.receipts import Receipt, load_receipt, write_receipt
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.fake import FakeProvider
from agent_evals.service.metrics import OUTCOMES, ServiceMetrics
from agent_evals.service.registry import PromptRegistry
from agent_evals.service.tracing import make_tracer, record_steps
from agent_evals.types import AgentResult, RunContext, Step
from conftest import BUCKETS_URL, make_case

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fake_gateway() -> ResilientClient:
    fake = FakeProvider(0.0, rng=random.Random(1), sleep=lambda _: None)
    return ResilientClient([Provider("fake", fake, CircuitBreaker(3, 30.0))])


def service(tmp_path: Path, corpus: Corpus, exporter: InMemorySpanExporter) -> TestClient:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    llm = fake_gateway()
    deps = Deps(
        settings=ServiceConfig(),
        registry=PromptRegistry.create(
            tmp_path / "r.yaml", tmp_path / "l.jsonl", SYSTEM_PROMPTS, "v3"
        ),
        agent_for=lambda _: DocsAgent(llm, corpus, DocsAgentConfig(prompt_version="v3")),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=5.0),
        budget=DailyBudget(tmp_path / "spend.json", 1.0),
        provider_pricing={"fake": Pricing()},
        tracer=provider.get_tracer("test"),
    )
    return TestClient(create_app(deps))


# ---- the fake provider -------------------------------------------------------------------------


def test_the_fake_provider_plays_the_whole_loop_through_the_real_agent(corpus: Corpus) -> None:
    agent = DocsAgent(fake_gateway(), corpus, DocsAgentConfig())
    result = agent.run(make_case(), RunContext(run_id="r", case_id="c", attempt=1))
    assert result.error is None
    assert [s.name for s in result.trajectory if s.kind == "tool"] == [
        "search_docs",
        "read_page",
        "submit_answer",
    ]
    assert result.output["citations"][0]["url"] == BUCKETS_URL
    assert result.output["citations"][0]["quote"] in corpus.pages[BUCKETS_URL].text


def test_the_fake_provider_abstains_when_the_search_finds_nothing() -> None:
    agent = DocsAgent(fake_gateway(), Corpus([]), DocsAgentConfig())
    result = agent.run(make_case(), RunContext(run_id="r", case_id="c", attempt=1))
    assert result.error is None and result.output["abstained"] is True


def test_the_fake_provider_waits_around_its_latency() -> None:
    waits: list[float] = []
    fake = FakeProvider(2.0, rng=random.Random(1), sleep=waits.append)
    messages = [{"role": "user", "content": "q"}]
    replies = [fake.create({"messages": messages}, salt="s") for _ in range(400)]
    mean = sum(waits) / len(waits)
    assert 2.0 < mean < 2.6  # lognormal around 2, with a slow tail
    assert max(waits) > 2 * mean
    assert replies[0].latency_ms == pytest.approx(waits[0] * 1000)


# ---- traces ------------------------------------------------------------------------------------


def test_an_answer_is_a_trace_of_its_steps_with_who_served_each_call(
    tmp_path: Path, corpus: Corpus
) -> None:
    exporter = InMemorySpanExporter()
    c = service(tmp_path, corpus, exporter)
    r = c.post("/v1/ask", json={"question": "How long can a bucket name be?"})
    assert r.status_code == 200, r.text
    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["ask"]
    assert root.attributes is not None
    assert root.attributes["request.id"] == r.json()["request_id"]
    assert root.attributes["prompt.version"] == "v3" and root.attributes["outcome"] == "answered"
    assert root.attributes["llm.model_calls"] == 3
    children = [s for s in exporter.get_finished_spans() if s.parent is not None]
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in children)
    assert len(children) == 6  # three model calls and three tool calls
    model = next(s for s in children if s.name.startswith("model"))
    assert model.attributes is not None
    assert model.attributes["llm.provider"] == "fake"
    assert model.attributes["llm.input_tokens"] == 1500
    starts = [s.start_time for s in children]
    assert starts == sorted(starts)  # laid end to end, in order


def test_a_trace_holds_no_question_query_or_quote(tmp_path: Path, corpus: Corpus) -> None:
    exporter = InMemorySpanExporter()
    c = service(tmp_path, corpus, exporter)
    secret = "bucket account 123456789012 private"
    c.post("/v1/ask", json={"question": secret})
    found = " ".join(
        f"{s.name} {dict(s.attributes or {})} {s.events}" for s in exporter.get_finished_spans()
    )
    assert "123456789012" not in found and "question.chars" in found


def test_a_refused_request_is_a_span_with_its_outcome(tmp_path: Path, corpus: Corpus) -> None:
    exporter = InMemorySpanExporter()
    c = service(tmp_path, corpus, exporter)
    assert c.post("/v1/ask", json={"question": "x" * 3000}).status_code == 422
    [span] = exporter.get_finished_spans()
    assert span.attributes is not None and span.attributes["outcome"] == "invalid"


def test_a_failed_step_is_marked_and_no_endpoint_means_no_exporter() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    step = Step(index=0, kind="tool", name="read_page", is_error=True, latency_ms=5.0)
    record_steps(provider.get_tracer("t"), 1000, AgentResult(trajectory=[step]), [])
    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.end_time == 1000 + 5_000_000
    assert not make_tracer(None).start_span("x").is_recording()
    assert not make_tracer("").start_span("x").is_recording()


# ---- gate receipts -----------------------------------------------------------------------------


def test_a_receipt_is_written_once_and_read_back(tmp_path: Path) -> None:
    fields = {
        "agent_version": "abc",
        "prompt_sha256": "def",
        "target": "judge_supported",
        "champion_run": "a",
        "challenger_run": "b",
    }
    path = write_receipt(**fields, directory=tmp_path)
    held = load_receipt("abc", tmp_path)
    assert held is not None and held.basis == "gate" and held.gated_version == "abc"
    first = path.read_text(encoding="utf-8")
    write_receipt(**{**fields, "challenger_run": "c"}, directory=tmp_path)
    assert path.read_text(encoding="utf-8") == first
    assert load_receipt("nothing", tmp_path) is None


def test_the_check_passes_today_and_fails_on_a_version_with_no_receipt(tmp_path: Path) -> None:
    check = _script("check_gate_receipt")
    config = load_config(ROOT / "configs" / "default.yaml")
    config = config.model_copy(
        update={
            "service": config.service.model_copy(
                update={"registry_path": ROOT / "configs" / "registry.yaml"}
            )
        }
    )
    versions = check.served_versions(config)
    assert list(versions) == ["champion"]
    assert check.missing(versions, ROOT / "results" / "gates" / "receipts") == []
    assert check.missing({"champion": "unmeasured"}, tmp_path) == [
        "the champion, agent version unmeasured, has no gate receipt"
    ]


def test_a_change_to_the_agents_behavior_changes_the_version_the_check_reads() -> None:
    check = _script("check_gate_receipt")
    base = DocsAgentConfig(prompt_version="v3")
    version = DocsAgent(check.NoModel(), Corpus([]), base).version
    for changed in ({"search_k": 9}, {"max_steps": 3}, {"prompt_version": "v4"}):
        other = base.model_copy(update=changed)
        assert DocsAgent(check.NoModel(), Corpus([]), other).version != version, changed


def test_the_carried_receipt_is_true() -> None:
    """The champion's gate ran on 8784b07dbd32. The receipt says only iteration 2's unused option
    and its rejection text moved the fingerprint: without those two it is that version exactly."""
    held = load_receipt("3b34d1c3829d", ROOT / "results" / "gates" / "receipts")
    assert held is not None and isinstance(held, Receipt)
    assert (held.basis, held.gated_version) == ("carried", "8784b07dbd32")
    config = load_config(ROOT / "configs" / "default.yaml").docs_agent
    assert config.prompt_version == "v3" and config.require_number_coverage is False
    check = _script("check_gate_receipt")
    agent = DocsAgent(check.NoModel(), Corpus([]), config)
    assert agent.version == held.agent_version
    assert sha256_text(agent.system_prompt)[:12] == held.prompt_sha256
    settings = config.model_dump(mode="json", exclude={"model": {"pricing"}})
    settings.pop("require_number_coverage")
    a = agent_module
    fingerprint = {
        "agent": agent.name,
        "config": settings,
        "prompts": {
            "system": agent.system_prompt,
            "nudge": a.NUDGE_V1,
            "invalid_answer": a.INVALID_ANSWER_V1,
            "rejected": a.REJECTED_V1,
            "reject_uncited": a.REJECT_UNCITED_V1,
            "reject_markup": a.REJECT_MARKUP_V1,
        },
        "tool_markup": a.TOOL_MARKUP.pattern,
        "tools": agent.tools,
    }
    assert sha256_text(canonical_json(fingerprint))[:12] == held.gated_version


# ---- the deployment files ----------------------------------------------------------------------


def metric_names() -> set[str]:
    names: set[str] = set()
    for family in ServiceMetrics().registry.collect():
        names |= {family.name, *(family.name + s for s in ("_total", "_bucket", "_count", "_sum"))}
    return names


def used_in(text: str) -> set[str]:
    """Metric names in a query. An outcome such as agent_failed is a label's value, not a metric."""
    found = re.findall(r"\b(?:ask|llm|gateway|agent|registry)_[a-z_]+\b", text)
    return set(found) - set(OUTCOMES)


def test_every_alert_and_dashboard_query_names_a_metric_the_service_exports() -> None:
    known = metric_names()
    alerts = (ROOT / "deploy" / "alerts.yml").read_text(encoding="utf-8")
    board = (ROOT / "deploy" / "grafana" / "dashboards" / "docs-agent.json").read_text("utf-8")
    assert used_in(alerts) - known == set()
    assert used_in(board) - known == set()


def test_every_alert_has_a_section_in_the_runbook() -> None:
    rules = yaml.safe_load((ROOT / "deploy" / "alerts.yml").read_text(encoding="utf-8"))
    names = [r["alert"] for g in rules["groups"] for r in g["rules"]]
    runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert names and [n for n in names if f"## {n}\n" not in runbook] == []


def test_the_committed_dashboard_is_what_the_script_writes() -> None:
    held = json.loads(
        (ROOT / "deploy" / "grafana" / "dashboards" / "docs-agent.json").read_text("utf-8")
    )
    assert held == _script("make_dashboard").dashboard()


def test_every_port_in_the_stack_is_bound_to_localhost() -> None:
    stack: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    ports = [p for s in stack["services"].values() for p in s.get("ports", [])]
    assert ports and all(str(p).startswith("127.0.0.1:") for p in ports)
    assert load_config(ROOT / "configs" / "service.yaml").service.host == "127.0.0.1"


def test_the_fake_config_is_the_real_one_but_for_the_fake_provider() -> None:
    real = load_config(ROOT / "configs" / "service.yaml")
    fake = load_config(ROOT / "configs" / "service-fake.yaml")
    assert real.service.fake_latency_s is None and fake.service.fake_latency_s == 1.5
    assert fake.docs_agent == real.docs_agent
    assert fake.service.max_concurrent == real.service.max_concurrent
