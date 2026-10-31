"""The judge rounds' spend rules (spend.Round, spend.round_for): judge v5's and judge v6's ledgers,
caps, estimates and pilot stops, and which one a claim-judge command spends from."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_evals import cli, spend
from agent_evals.harness import llm as llm_module
from agent_evals.harness.manifest import file_sha256
from agent_evals.store.db import ResultsStore
from agent_evals.suites import write_suite
from agent_evals.types import AgentResult
from conftest import BUCKETS_URL, ScriptedLLM, make_case, tool_use, write_pilot

QUOTE = "Bucket names must be between 3 and 63 characters long."
SPAN = "between 3 and 63 characters long"
PRICED = "{input_per_mtok: 2.0, output_per_mtok: 10.0}"
CALL_USD = (100 * 2.0 + 20 * 10.0) / 1_000_000  # each ScriptedLLM reply: 100 tokens in, 20 out


def test_each_round_has_its_own_ledger_cap_estimate_and_stop() -> None:
    v5, v6 = spend.round_for("judge-v5"), spend.round_for("judge-v6")
    assert (v5.path, v5.cap_usd, v5.estimate_per_answer_usd, v5.stop_factor) == (
        spend.DEFAULT_PATH,
        60.0,
        0.02,
        1.5,
    )
    assert (v6.path, v6.cap_usd, v6.estimate_per_answer_usd, v6.stop_factor) == (
        spend.V6_PATH,
        60.0,
        0.012,
        1.3,
    )
    assert spend.V6_PATH != spend.DEFAULT_PATH and v6.folder == spend.V6_PATH.parent
    assert v6.ledger().cap_usd == 60.0 and v6.ledger().path == spend.V6_PATH
    # The design's figures: at 1.3 times $0.012 the planned judgings reach about $57, under the
    # cap; v5's 1.5 would admit a cost at which they pass it.
    assert 3_494 * 1.3 * 0.012 < 60.0 < 3_494 * 1.5 * 0.012
    for name in ("judge", "judge-v4", "judge-v7", "judge-v6-shadow"):
        with pytest.raises(ValueError, match="belongs to no approved judge round"):
            spend.round_for(name)


def test_the_v6_pilot_stop_is_its_own(tmp_path: Path) -> None:
    v6 = spend.round_for("judge-v6")
    write_pilot(0.0155, folder=v6.folder)  # under 1.3 x 0.012 = 0.0156
    assert v6.pilot_cost() == 0.0155
    assert spend.pilot_cost(v6.folder) == 0.0155  # v5's rule (1.5 x 0.02) would pass it too
    (v6.folder / spend.PILOT_FILE).unlink()
    write_pilot(0.0157, folder=v6.folder)  # over v6's stop, under v5's
    with pytest.raises(spend.PilotRefusal, match=r"more than 1\.3 times the \$0\.012 estimate"):
        v6.pilot_cost()
    assert spend.pilot_cost(v6.folder) == 0.0157
    (v6.folder / spend.PILOT_FILE).unlink()
    with pytest.raises(spend.PilotRefusal, match="the pilot comes first"):
        v6.pilot_cost()


def _judged_run(tmp_path: Path, grader_name: str) -> Path:
    suite = tmp_path / "suite.jsonl"
    write_suite(suite, [make_case()])
    output = {"answer": QUOTE, "citations": [{"url": BUCKETS_URL, "quote": QUOTE}]}
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        manifest = {"suite": {"path": str(suite), "sha256": file_sha256(suite)}}
        store.start_run(
            "r1", agent="docs", agent_version="v", suite="smoke", attempts=1, manifest=manifest
        )
        store.record_attempt("r1", "t-1", 1, AgentResult(output=output), 0.0, [])
    finally:
        store.close()
    config = tmp_path / "config.yaml"
    config.write_text(
        f'paths:\n  db: "{(tmp_path / "evals.duckdb").as_posix()}"\n'
        f'  cache_dir: "{(tmp_path / "cache").as_posix()}"\n'
        f"judge:\n  method: claims\n  grader_name: {grader_name}\n  rubric_version: v5\n"
        f"  model:\n    name: claude-sonnet-5-5\n    pricing: {PRICED}\n",
        encoding="utf-8",
    )
    return config


def _claim_judge() -> ScriptedLLM:
    claim = {
        "answer_span": SPAN,
        "role": "core",
        "support": [{"quote": 1, "quote_span": SPAN}],
        "score": 90,
        "why": "quoted",
    }
    search = {"found": False, "answer_span": "", "role": "core", "why": "", "confidence": 0}
    return ScriptedLLM(
        [
            [tool_use("submit_claims", {"claims": [claim], "not_claims": []})],
            [tool_use("submit_skeptic", {"flagged": [], "search": search})],
        ]
    )


def _invoke(monkeypatch: pytest.MonkeyPatch, llm: Any, *args: str) -> tuple[int, str]:
    from typer.testing import CliRunner

    monkeypatch.setattr(llm_module, "AnthropicClient", lambda *a, **k: llm)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, " ".join(result.output.split())


def test_evals_judge_spends_from_the_round_its_grader_name_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _judged_run(tmp_path, "judge-v6")
    v6 = spend.round_for("judge-v6")
    code, out = _invoke(monkeypatch, _claim_judge(), "judge", "r1", "-c", str(config))
    assert code == 2 and "the pilot comes first" in out  # v6's own pilot, not v5's
    write_pilot(0.02, folder=spend.DEFAULT_PATH.parent)  # v5's pilot does not count for v6
    code, out = _invoke(monkeypatch, _claim_judge(), "judge", "r1", "-c", str(config))
    assert code == 2 and "the pilot comes first" in out
    write_pilot(0.011, folder=v6.folder)
    code, out = _invoke(monkeypatch, _claim_judge(), "judge", "r1", "-c", str(config))
    assert code == 0, out
    [entry] = v6.ledger().entries()
    assert (entry.step, entry.calls, entry.uncached_calls) == ("evals judge r1", 2, 2)
    assert entry.usd == pytest.approx(2 * CALL_USD)
    assert not spend.DEFAULT_PATH.exists()  # nothing went to v5's ledger
    held = json.loads(spend.V6_PATH.read_text(encoding="utf-8"))
    assert held["cap_usd"] == 60.0 and "The judge round has spent $0.00 of its $60.00 cap" in out


def test_evals_judge_refuses_a_claim_judge_of_no_approved_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _judged_run(tmp_path, "judge-v7")
    llm = _claim_judge()
    code, out = _invoke(monkeypatch, llm, "judge", "r1", "-c", str(config))
    assert code == 2 and "belongs to no approved judge round" in out
    assert llm.requests == [] and not spend.V6_PATH.exists() and not spend.DEFAULT_PATH.exists()
