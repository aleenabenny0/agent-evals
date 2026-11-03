"""Measuring the failover path, and the tools of the drills, offline (phase 8, parts 3 and 6)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_evals import cli
from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.config import Config, GatewayConfig, ProviderConfig, ServiceConfig
from agent_evals.corpus import Corpus
from agent_evals.service.fake import FakeProvider
from agent_evals.spend import SpendCapError, SpendLedger
from agent_evals.store.db import ResultsStore

ROOT = Path(__file__).resolve().parents[1]
FALLBACK = ProviderConfig(
    name="second", base_url="http://127.0.0.1:1/v1", api_key_env="SECOND_KEY", model="second-model"
)


def script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def with_fallback(held: ProviderConfig | None) -> Config:
    return Config(service=ServiceConfig(gateway=GatewayConfig(fallback=held)))


def test_a_fallback_run_needs_a_fallback_in_the_config() -> None:
    with pytest.raises(typer.Exit):
        cli._fallback(with_fallback(None))


def test_a_fallback_run_needs_the_key_and_names_only_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SECOND_KEY", raising=False)
    with pytest.raises(typer.Exit):
        cli._fallback(with_fallback(FALLBACK))
    assert "SECOND_KEY" in capsys.readouterr().out
    monkeypatch.setenv("SECOND_KEY", "a-key")
    assert cli._fallback(with_fallback(FALLBACK)) is FALLBACK


def fallback_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n'
        f'  cache_dir: "{(tmp_path / "cache").as_posix()}"\n'
        "service:\n  gateway:\n    fallback:\n      name: second\n"
        "      base_url: http://127.0.0.1:1/v1\n      api_key_env: SECOND_KEY\n"
        "      model: second-model\n",
        encoding="utf-8",
    )
    return path


def test_a_fallback_run_says_so_in_its_manifest_with_the_agent_version_unchanged(
    tmp_path: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECOND_KEY", "a-key")
    monkeypatch.setattr(Corpus, "load", lambda *_: corpus)
    monkeypatch.setattr(cli, "fallback_client", lambda held, cache: FakeProvider(0.0))
    config = fallback_config(tmp_path)
    args = ["run", str(ROOT / "suites/docs/smoke.jsonl"), "--limit", "1", "--on-fallback"]
    result = CliRunner().invoke(cli.app, [*args, "--allow-drafts", "-c", str(config)])
    assert result.exit_code == 0, result.output
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        run_id = store.con.execute("SELECT run_id FROM runs").fetchone()[0]  # type: ignore[index]
        info = store.run_info(run_id)
    finally:
        store.close()
    assert info["manifest"]["provider"] == {"name": "second", "model": "second-model"}
    assert (
        info["agent_version"] == DocsAgent(FakeProvider(0.0), corpus, Config().docs_agent).version
    )


def test_the_ledger_takes_a_step_it_did_not_meter_and_still_holds_the_cap(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", 10.0)
    ledger.add("drill", calls=300, usd=9.5)
    assert ledger.total_usd() == 9.5
    assert ledger.entries()[0].calls == 300
    ledger.check("small", 0.5)
    with pytest.raises(SpendCapError):
        ledger.check("too big", 0.51)


# ---- the drill's record ------------------------------------------------------------------------


def row(
    sent_s: float, role: str, *, abstained: bool = False, status: int = 200, pages: int = 1
) -> dict[str, Any]:
    if status != 200:
        return {"sent_s": sent_s, "status": status, "outcome": "overloaded"}
    return {
        "sent_s": sent_s,
        "status": 200,
        "role": role,
        "abstained": abstained,
        "citations": 0 if abstained else 1,
        "pages_read": pages,
        "cost_usd": 0.01,
    }


def test_a_drills_summary_grades_as_the_rule_does_and_times_the_rollback() -> None:
    rows = [
        row(1, "champion"),
        row(2, "canary", abstained=True),
        row(3, "champion", status=429),
        row(20, "canary", pages=0),
        row(50, "champion"),
    ]
    held = script("drill").summary(rows, canary_at=1.5, rolled_at=31.5)
    assert held["roles"] == {
        "canary": {"answers": 2, "bad": 2, "read_no_page": 1},
        "champion": {"answers": 2, "bad": 0, "read_no_page": 0},
    }
    assert held["statuses"] == {"200": 4, "429": 1}
    assert held["seconds_to_rollback"] == 30.0
    assert held["canary_answers_sent_after_rollback"] == 0
    assert held["cost_seen_usd"] == 0.04


def test_a_drill_counts_a_canary_answer_sent_after_the_rollback() -> None:
    held = script("drill").summary([row(40, "canary")], canary_at=1.0, rolled_at=30.0)
    assert held["canary_answers_sent_after_rollback"] == 1


def test_a_drill_with_no_rollback_says_so() -> None:
    held = script("drill").summary([row(1, "champion")], canary_at=None, rolled_at=None)
    assert held["seconds_to_rollback"] is None and held["rollback_seen_s"] is None


def test_a_drill_cannot_release_a_prompt_outside_a_drill_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["drill.py", "--label", "x", "--canary", "drill-fault", "-c", "configs/service.yaml"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.chdir(ROOT)
    with pytest.raises(SystemExit, match=r"does not set service.drill"):
        script("drill").main()


# ---- the gate in front of the primary ----------------------------------------------------------


def gate(reply: httpx.Response) -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply

    upstream = httpx.AsyncClient(
        base_url="https://upstream.test", transport=httpx.MockTransport(handler)
    )
    return TestClient(script("primary_gate").create_gate(upstream)), seen


def test_the_gate_forwards_a_call_unchanged_and_returns_the_reply() -> None:
    c, seen = gate(httpx.Response(200, json={"id": "msg_1"}))
    body = {"model": "m", "messages": []}
    reply = c.post("/v1/messages", json=body, headers={"x-api-key": "a-key", "accept": "json"})
    assert reply.status_code == 200 and reply.json() == {"id": "msg_1"}
    assert str(seen[0].url) == "https://upstream.test/v1/messages"
    assert seen[0].headers["x-api-key"] == "a-key"
    assert json.loads(seen[0].content) == body
    assert seen[0].headers["host"] == "upstream.test"  # not the gate's own


def test_the_gate_passes_a_providers_error_through() -> None:
    c, _ = gate(httpx.Response(529, json={"error": "overloaded"}))
    assert c.post("/v1/messages", json={}).status_code == 529


def test_the_failover_drills_compose_file_publishes_no_port() -> None:
    import yaml

    stack = yaml.safe_load((ROOT / "deploy" / "failover-drill.yml").read_text(encoding="utf-8"))
    assert "ports" not in stack["services"]["primary-gate"]
    assert stack["services"]["service"]["environment"]["ANTHROPIC_BASE_URL"].startswith("http://")
