"""The gateway and the OpenAI-compatible client (docs/plans/phase-8-llm-ops.md, part 3), offline:
the second provider is a mock HTTP transport, and the real docs agent runs over both."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.config import DocsAgentConfig, Pricing, ProviderConfig, ServiceConfig
from agent_evals.corpus import Corpus
from agent_evals.harness.cache import CallCache
from agent_evals.harness.gateway import (
    AllProvidersFailed,
    CircuitBreaker,
    GatewayEvent,
    Provider,
    ResilientClient,
)
from agent_evals.harness.llm import LLMResponse, usage_from
from agent_evals.harness.openai_compat import (
    OpenAICompatibleClient,
    ProviderHTTPError,
    from_openai,
    to_openai,
)
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.metrics import ServiceMetrics
from agent_evals.service.registry import PromptRegistry
from agent_evals.types import RunContext
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use

GOOD_ANSWER = {
    "answer": "Bucket names must be between 3 and 63 characters long.",
    "citations": [
        {"url": BUCKETS_URL, "quote": "Bucket names must be between 3 and 63 characters long."}
    ],
    "abstained": False,
    "confidence": 0.9,
}
TOOLS = [
    {
        "name": "search_docs",
        "description": "Search.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "strict": True,
    }
]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Down:
    """A provider that fails every call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("connection refused")
        self.calls = 0

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        self.calls += 1
        raise self.error


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def ok(replies: int = 9) -> ScriptedLLM:
    return ScriptedLLM([[text(f"reply {n}")] for n in range(replies)])


def gateway(
    *clients: Any, threshold: int = 2, clock: Clock | None = None
) -> tuple[ResilientClient, list[GatewayEvent]]:
    events: list[GatewayEvent] = []
    providers = [
        Provider(f"p{n}", client, CircuitBreaker(threshold, 30.0, clock=clock or Clock()))
        for n, client in enumerate(clients)
    ]
    return ResilientClient(providers, on_event=events.append), events


REQUEST: dict[str, Any] = {"model": "m", "max_tokens": 10, "messages": []}


def kinds(events: list[GatewayEvent]) -> list[tuple[str, str]]:
    return [(e.kind, e.provider) for e in events]


# ---- the breaker -------------------------------------------------------------------------------


def test_the_breaker_opens_after_repeated_failures_and_tests_with_one_call() -> None:
    clock = Clock()
    breaker = CircuitBreaker(2, 30.0, clock=clock)
    assert breaker.allow() and breaker.state == "closed"
    breaker.failure()
    assert breaker.state == "closed"  # one failure in a row is not an outage
    breaker.failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.now = 29.0
    assert not breaker.allow()
    clock.now = 30.0
    assert breaker.state == "half_open"
    assert breaker.allow() and not breaker.allow()  # one caller tests it; the rest wait
    breaker.failure()
    assert breaker.state == "open" and not breaker.allow()  # the test failed: a new cool-down
    clock.now = 60.0
    assert breaker.allow()
    breaker.success()
    assert breaker.state == "closed" and breaker.allow()


def test_a_success_clears_the_failures_in_a_row() -> None:
    breaker = CircuitBreaker(2, 30.0, clock=Clock())
    breaker.failure()
    breaker.success()
    breaker.failure()
    assert breaker.state == "closed"


# ---- failover ----------------------------------------------------------------------------------


def test_a_healthy_primary_serves_alone() -> None:
    primary, second = ok(), ok()
    client, events = gateway(primary, second)
    assert client.create(REQUEST, salt="s").content == [text("reply 0")]
    assert len(primary.requests) == 1 and second.requests == [] and events == []


def test_a_failed_call_goes_to_the_next_provider_and_is_reported() -> None:
    primary, second = Down(), ok()
    client, events = gateway(primary, second)
    assert client.create(REQUEST, salt="s").content == [text("reply 0")]
    assert kinds(events) == [("failure", "p0"), ("failover", "p1")]
    assert events[0].detail == "RuntimeError"


def test_an_open_circuit_stops_the_wait_on_a_provider_that_is_down() -> None:
    primary, second = Down(), ok()
    client, events = gateway(primary, second)
    for _ in range(5):
        client.create(REQUEST, salt="s")
    assert primary.calls == 2  # then skipped, so an outage costs two timeouts, not five
    assert ("circuit_open", "p0") in kinds(events)
    assert client.available()


def test_the_primary_comes_back_after_the_cool_down() -> None:
    clock = Clock()
    down = Down()
    second = ok()

    class Recovers:
        healthy = False

        def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
            if self.healthy:
                return ok(1).create(request, salt=salt)
            return down.create(request, salt=salt)

    primary = Recovers()
    client, events = gateway(primary, second, clock=clock)
    client.create(REQUEST, salt="s")
    client.create(REQUEST, salt="s")
    primary.healthy = True
    clock.now = 31.0
    second_calls = len(second.requests)
    client.create(REQUEST, salt="s")
    assert len(second.requests) == second_calls  # the primary took the test call
    assert kinds(events)[-1] == ("circuit_closed", "p0")


def test_with_every_provider_down_the_call_fails_and_says_why() -> None:
    client, _ = gateway(Down(), Down(), threshold=1)
    with pytest.raises(AllProvidersFailed, match="p0: RuntimeError; p1: RuntimeError"):
        client.create(REQUEST, salt="s")
    assert not client.available()
    with pytest.raises(AllProvidersFailed, match="p0: circuit open; p1: circuit open"):
        client.create(REQUEST, salt="s")


def test_a_refused_request_is_a_bug_here_so_it_trips_nothing_and_does_not_fail_over() -> None:
    primary, second = Down(StatusError(400)), ok()
    client, events = gateway(primary, second, threshold=1)
    for _ in range(3):
        with pytest.raises(StatusError):
            client.create(REQUEST, salt="s")
    assert primary.calls == 3 and second.requests == [] and events == []
    assert client.providers[0].breaker.state == "closed"


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 503, 529])
def test_a_provider_failure_status_fails_over(status: int) -> None:
    client, events = gateway(Down(StatusError(status)), ok())
    client.create(REQUEST, salt="s")
    assert kinds(events)[-1] == ("failover", "p1")


# ---- translation -------------------------------------------------------------------------------


def test_a_request_with_a_whole_conversation_translates_to_the_openai_shape() -> None:
    request = {
        "model": "claude-haiku-5-5",
        "max_tokens": 100,
        "system": "Answer with citations.",
        "tools": TOOLS,
        "messages": [
            {"role": "user", "content": "How long?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hm", "signature": "x"},
                    text("Searching."),
                    tool_use("search_docs", {"query": "bucket"}, "t1"),
                    tool_use("search_docs", {"query": "name"}, "t2"),
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "hit"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": True},
                    {"type": "text", "text": "Submit now."},
                ],
            },
        ],
    }
    body = to_openai(request, "gpt-small")
    assert body["model"] == "gpt-small" and body["max_completion_tokens"] == 100
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search_docs",
                "description": "Search.",
                "parameters": TOOLS[0]["input_schema"],
            },
        }
    ]
    system, user, assistant, tool_1, tool_2, nudge = body["messages"]
    assert system == {"role": "system", "content": "Answer with citations."}
    assert user == {"role": "user", "content": "How long?"}
    assert assistant["content"] == "Searching."  # the thinking block has no counterpart
    assert [c["id"] for c in assistant["tool_calls"]] == ["t1", "t2"]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"query": "bucket"}
    assert tool_1 == {"role": "tool", "tool_call_id": "t1", "content": "hit"}
    assert tool_2["content"] == "ERROR: x"  # the model must see it failed
    assert nudge == {"role": "user", "content": "Submit now."}
    assert to_openai(request, "m", "max_tokens")["max_tokens"] == 100


def test_a_reply_translates_to_the_shape_the_agent_reads() -> None:
    reply = from_openai(
        {
            "model": "gpt-small",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Searching.",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "search_docs", "arguments": '{"query": "x"}'},
                            },
                            {
                                "id": "c2",
                                "function": {"name": "read_page", "arguments": "{bad json"},
                            },
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        }
    )
    assert reply["stop_reason"] == "tool_use"
    assert reply["content"] == [
        {"type": "text", "text": "Searching."},
        {"type": "tool_use", "id": "c1", "name": "search_docs", "input": {"query": "x"}},
        {"type": "tool_use", "id": "c2", "name": "read_page", "input": {}},  # the agent rejects it
    ]
    usage = usage_from(LLMResponse.model_validate({**reply, "latency_ms": 5.0}))
    assert (usage.input_tokens, usage.output_tokens, usage.prompt_cache_read_tokens) == (
        100,
        20,
        30,
    )


@pytest.mark.parametrize(
    ("finish", "stop"),
    [("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "refusal")],
)
def test_each_way_a_reply_can_end_is_read(finish: str, stop: str) -> None:
    data = {"choices": [{"finish_reason": finish, "message": {"content": "x"}}]}
    assert from_openai(data)["stop_reason"] == stop


# ---- the HTTP client ---------------------------------------------------------------------------


def completion(*calls: tuple[str, dict[str, Any]], words: str = "") -> dict[str, Any]:
    tool_calls = [
        {"id": f"call_{n}", "function": {"name": name, "arguments": json.dumps(args)}}
        for n, (name, args) in enumerate(calls)
    ]
    message: dict[str, Any] = {"content": words or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": "gpt-small",
        "choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class Server:
    """A mock OpenAI-compatible server, replying from a list, recording what it was sent."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json=self.replies.pop(0))

    def client(self, cache: CallCache | None = None) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            base_url="https://second.example.test/v1",
            api_key="test-key",
            model="gpt-small",
            transport=httpx.MockTransport(self),
            cache=cache,
        )


def test_the_client_posts_to_chat_completions_with_the_key_and_reads_the_reply() -> None:
    server = Server(completion(words="hello"))
    resp = server.client().create(
        {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}, salt="s"
    )
    assert resp.content == [{"type": "text", "text": "hello"}] and resp.stop_reason == "end_turn"
    sent = server.seen[0]
    assert str(sent.url) == "https://second.example.test/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer test-key"


def test_an_error_status_carries_its_code_for_the_gateway() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    client = OpenAICompatibleClient(
        base_url="https://x.test", api_key="k", model="m", transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(ProviderHTTPError) as raised:
        client.create(REQUEST, salt="s")
    assert raised.value.status_code == 503 and "overloaded" in str(raised.value)


def test_the_client_uses_the_call_cache_so_an_eval_on_it_is_free_to_rerun(tmp_path: Path) -> None:
    server = Server(completion(words="hello"))
    client = server.client(CallCache(tmp_path))
    request = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    assert not client.create(request, salt="s").cache_hit
    again = client.create(request, salt="s")
    assert again.cache_hit and again.content == [{"type": "text", "text": "hello"}]
    assert len(server.seen) == 1


# ---- the real agent over both ------------------------------------------------------------------


def run_agent(llm: Any, corpus: Corpus) -> Any:
    agent = DocsAgent(llm, corpus, DocsAgentConfig())
    return agent.run(make_case(), RunContext(run_id="r", case_id="c", attempt=1))


def test_the_agent_finishes_an_answer_entirely_on_the_second_provider(corpus: Corpus) -> None:
    server = Server(
        completion(("search_docs", {"query": "bucket name length"})),
        completion(("read_page", {"url": BUCKETS_URL, "start": 0})),
        completion(("submit_answer", GOOD_ANSWER)),
    )
    result = run_agent(server.client(), corpus)
    assert result.error is None and result.output == GOOD_ANSWER
    assert [s.name for s in result.trajectory if s.kind == "tool"] == [
        "search_docs",
        "read_page",
        "submit_answer",
    ]
    last = json.loads(server.seen[-1].content)["messages"]
    assert last[-1]["role"] == "tool" and "63" in last[-1]["content"]


def test_an_answer_three_steps_in_continues_on_the_next_provider(corpus: Corpus) -> None:
    """The primary serves the first two steps and goes down; the second provider, which has seen
    nothing, gets the whole conversation so far in its own shape and finishes the answer."""

    class DiesAfterTwo:
        def __init__(self) -> None:
            self.live = ScriptedLLM(
                [
                    [tool_use("search_docs", {"query": "bucket name length"}, "t1")],
                    [tool_use("read_page", {"url": BUCKETS_URL, "start": 0}, "t2")],
                ]
            )

        def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
            if not self.live.script:
                raise RuntimeError("connection reset")
            return self.live.create(request, salt=salt)

    server = Server(completion(("submit_answer", GOOD_ANSWER)))
    client, events = gateway(DiesAfterTwo(), server.client())
    result = run_agent(client, corpus)
    assert result.error is None and result.output == GOOD_ANSWER
    assert kinds(events) == [("failure", "p0"), ("failover", "p1")]
    sent = json.loads(server.seen[0].content)["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert sent[2]["tool_calls"][0]["id"] == "t1" and sent[3]["tool_call_id"] == "t1"


# ---- the service over the gateway --------------------------------------------------------------


def service(tmp_path: Path, llm: ResilientClient, corpus: Corpus, metrics: ServiceMetrics) -> Any:
    prompts = {"v3": "prompt"}
    held = PromptRegistry.create(tmp_path / "r.yaml", tmp_path / "l.jsonl", prompts, "v3")
    settings = DocsAgentConfig(prompt_version="v3")
    deps = Deps(
        settings=ServiceConfig(),
        registry=held,
        agent_for=lambda _: DocsAgent(llm, corpus, settings),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=5.0),
        budget=DailyBudget(tmp_path / "spend.json", 1.0),
        metrics=metrics,
        ready_checks={"provider": llm.available},
        provider_pricing={"p1": Pricing(input_per_mtok=2.0, output_per_mtok=10.0)},
    )
    return TestClient(create_app(deps))


def test_a_failover_shows_in_the_answer_the_cost_and_the_metrics(
    tmp_path: Path, corpus: Corpus
) -> None:
    server = Server(completion(("submit_answer", GOOD_ANSWER)))
    metrics = ServiceMetrics()
    events: list[GatewayEvent] = []

    def both(event: GatewayEvent) -> None:
        events.append(event)
        metrics.on_gateway_event(event)

    llm = ResilientClient(
        [
            Provider("p0", Down(), CircuitBreaker(1, 30.0, clock=Clock())),
            Provider("p1", server.client(), CircuitBreaker(1, 30.0, clock=Clock())),
        ],
        on_event=both,
    )
    c = service(tmp_path, llm, corpus, metrics)
    body = c.post("/v1/ask", json={"question": "How long can a bucket name be?"}).json()
    assert body["providers"] == ["p1"] and body["answer"] == GOOD_ANSWER["answer"]
    assert body["cost_usd"] == pytest.approx((100 * 2 + 20 * 10) / 1e6)  # at the second's prices
    text_ = c.get("/metrics").text
    assert 'gateway_failovers_total{provider="p1"} 1.0' in text_
    assert 'gateway_circuit_open{provider="p0"} 1.0' in text_
    assert 'gateway_provider_failures_total{kind="RuntimeError",provider="p0"} 1.0' in text_


def test_with_every_provider_down_the_service_says_so_and_stops_being_ready(
    tmp_path: Path, corpus: Corpus
) -> None:
    llm, _ = gateway(Down(), Down(), threshold=1)
    c = service(tmp_path, llm, corpus, ServiceMetrics())
    r = c.post("/v1/ask", json={"question": "q"})
    assert r.status_code == 502 and r.json()["error"] == "provider_error"
    assert c.get("/readyz").status_code == 503


def test_a_fallback_config_names_its_key_by_environment_variable_only() -> None:
    provider = ProviderConfig(
        name="second", base_url="https://x.test/v1", api_key_env="SECOND_API_KEY", model="m"
    )
    assert provider.api_key_env == "SECOND_API_KEY" and "key" not in provider.model_dump()
