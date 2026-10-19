"""The API's prompt cache: asked for on every call sent, invisible to the local call cache, and
counted in tokens and cost at its own price."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from agent_evals.config import Pricing
from agent_evals.harness.cache import CallCache
from agent_evals.harness.llm import AnthropicClient, usage_from, with_prompt_cache
from agent_evals.store.db import ResultsStore
from agent_evals.types import AgentResult, Usage

REQUEST: dict[str, Any] = {
    "model": "m",
    "max_tokens": 10,
    "system": "You answer questions about AWS.",
    "messages": [{"role": "user", "content": "hi"}],
}


class _Response:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._data


class _Messages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        usage = {
            "input_tokens": 50,
            "output_tokens": 5,
            "cache_creation_input_tokens": 600,
            "cache_read_input_tokens": 0,
        }
        return _Response(
            {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "usage": usage}
        )


class _SDK:
    def __init__(self) -> None:
        self.messages = _Messages()


def test_every_call_sent_asks_for_the_prompt_cache_and_the_local_key_is_unchanged(
    tmp_path: Path,
) -> None:
    cache = CallCache(tmp_path / "on")
    client = AnthropicClient(cache)
    sdk = _SDK()
    client._client = sdk
    first = client.create(REQUEST, salt="a")
    sent = sdk.messages.calls[0]
    assert sent["system"] == [
        {"type": "text", "text": REQUEST["system"], "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in sent  # one message: nothing past the system prompt is cached
    assert REQUEST["system"] == "You answer questions about AWS."  # the request is left alone
    assert cache.get(CallCache.key(REQUEST, "a")) is not None  # stored under the same key as before
    again = client.create(REQUEST, salt="a")
    assert again.cache_hit and len(sdk.messages.calls) == 1  # replayed from the local cache
    usage = usage_from(first)
    assert usage.input_tokens == 650  # the whole prompt: 50 fresh + 600 written to the cache
    assert (usage.prompt_cache_write_tokens, usage.prompt_cache_read_tokens) == (600, 0)
    off = AnthropicClient(CallCache(tmp_path / "off"), prompt_cache=False)
    off._client = sdk
    off.create(REQUEST, salt="b")
    assert sdk.messages.calls[1]["system"] == REQUEST["system"]  # sent exactly as given


def test_a_conversation_also_gets_automatic_caching_for_its_earlier_turns() -> None:
    turn_two = {
        **REQUEST,
        "system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "searching"}]},
            {"role": "user", "content": [{"type": "text", "text": "page"}]},
        ],
    }
    sent = with_prompt_cache(turn_two)
    assert sent["cache_control"] == {"type": "ephemeral"}  # the breakpoint moves with the turn
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}  # plus the shared prefix
    assert "cache_control" not in sent["system"][0]
    assert "cache_control" not in turn_two["system"][-1] and "cache_control" not in turn_two


def test_prompt_cache_tokens_are_billed_at_their_own_multiples() -> None:
    pricing = Pricing(input_per_mtok=1.0, output_per_mtok=5.0)
    usage = Usage(
        input_tokens=1000,
        output_tokens=100,
        prompt_cache_write_tokens=200,
        prompt_cache_read_tokens=600,
    )
    # 200 fresh + 200 written x 1.25 + 600 read x 0.1 = 510 at the input price, + 100 x 5 out.
    assert usage.cost_usd(pricing) == pytest.approx(1010 / 1_000_000)
    plain = Usage(input_tokens=1000, output_tokens=100)
    assert plain.cost_usd(pricing) == pytest.approx(1500 / 1_000_000)  # no cache: as before
    total = usage.add(usage)
    assert (total.input_tokens, total.prompt_cache_read_tokens) == (2000, 1200)


def test_an_existing_results_store_gains_the_prompt_cache_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(  # the attempts table as it was before prompt caching
        "CREATE TABLE attempts (run_id VARCHAR, case_id VARCHAR, attempt INTEGER, output VARCHAR, "
        "trajectory VARCHAR, input_tokens BIGINT, output_tokens BIGINT, model_calls INTEGER, "
        "cached_calls INTEGER, cost_usd DOUBLE, latency_ms DOUBLE, error VARCHAR, "
        "PRIMARY KEY (run_id, case_id, attempt))"
    )
    con.close()
    store = ResultsStore(path)
    try:
        store.start_run("r", agent="a", agent_version="v", suite="s", attempts=1, manifest={})
        usage = Usage(
            input_tokens=1000,
            output_tokens=10,
            prompt_cache_write_tokens=200,
            prompt_cache_read_tokens=600,
            model_calls=1,
        )
        store.record_attempt("r", "c", 1, AgentResult(output={}, usage=usage), 0.001, [])
        totals = store.totals("r")
        assert totals["input_tokens"] == 1000
        assert (totals["prompt_cache_write_tokens"], totals["prompt_cache_read_tokens"]) == (
            200,
            600,
        )
    finally:
        store.close()
