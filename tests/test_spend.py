"""The judge round's spend ledger (agent_evals.spend): only the calls the call cache did not serve
cost money, a step whose estimate would pass the cap is refused before it starts, and a step that
reaches the cap is stopped. Also `evals judge` and `evals judge-probe` with the claim judge, whose
spend is the round's."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals import cli, spend
from agent_evals.config import Pricing
from agent_evals.harness import llm as llm_module
from agent_evals.harness.llm import LLMResponse, usage_from
from agent_evals.harness.manifest import file_sha256
from agent_evals.spend import (
    APPROVED_CAP_USD,
    DEFAULT_PATH,
    ESTIMATE_PER_ANSWER_USD,
    MeteredLLM,
    SpendCapError,
    SpendLedger,
    cost_per_answer,
)
from agent_evals.store.db import ResultsStore
from agent_evals.suites import write_suite
from agent_evals.types import AgentResult, Grade
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use

# claude-sonnet-5-5's rates as the judge configs hold them
PRICING = Pricing(input_per_mtok=2.0, output_per_mtok=10.0, cache_read_multiplier=0.05)


class Replay:
    """An LLMClient that hands back the given responses in order."""

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.salts: list[str] = []

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        self.salts.append(salt)
        return self.responses.pop(0)


def response(
    input_tokens: int, output_tokens: int, *, cache_hit: bool = False, read: int = 0
) -> LLMResponse:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": read,
    }
    return LLMResponse(content=[], usage=usage, cache_hit=cache_hit)


def metered(*responses: LLMResponse) -> MeteredLLM:
    """A meter that has made one call for each response."""
    meter = MeteredLLM(Replay(*responses), PRICING)
    for _ in responses:
        meter.create({}, salt="s")
    return meter


def test_the_meter_prices_only_the_calls_the_call_cache_did_not_serve() -> None:
    sent = response(1_000, 200, read=4_000)  # the API's prompt cache read most of this prompt
    served = response(50_000, 9_000, cache_hit=True)  # replayed from the call cache: no spend
    llm = Replay(sent, served)
    meter = MeteredLLM(llm, PRICING)
    assert meter.create({"model": "m"}, salt="judge#claims#a") is sent
    assert meter.create({"model": "m"}, salt="judge#claims#b") is served
    assert llm.salts == ["judge#claims#a", "judge#claims#b"]
    assert (meter.calls, meter.uncached.model_calls) == (2, 1)
    # 1,000 input tokens at $2 a million, 4,000 read from the prompt cache at 0.05 times that,
    # and 200 output tokens at $10 a million
    assert meter.uncached_usd == pytest.approx((2_000 + 400 + 2_000) / 1_000_000)
    assert meter.uncached_usd == pytest.approx(usage_from(sent).cost_usd(PRICING))


def test_a_meter_needs_pricing_to_count_spend() -> None:
    with pytest.raises(ValueError, match="no pricing"):
        MeteredLLM(Replay(), Pricing())
    with pytest.raises(ValueError, match="no pricing"):
        MeteredLLM(Replay(), Pricing(input_per_mtok=2.0))


def test_the_first_record_creates_the_file_with_each_steps_calls_and_spend(tmp_path: Path) -> None:
    assert DEFAULT_PATH.as_posix() == "results/judge-v5/spend.json"
    path = tmp_path / "judge-v5" / "spend.json"
    ledger = SpendLedger(path)
    assert ledger.cap_usd == APPROVED_CAP_USD == 60.0
    ledger.check("pilot", 1.20)
    assert not path.exists() and ledger.entries() == []  # a check writes nothing
    replayed = response(1_000, 100, cache_hit=True)
    entry = ledger.record("pilot", metered(response(1_000, 100), replayed))
    assert (entry.step, entry.calls, entry.uncached_calls) == ("pilot", 2, 1)
    assert entry.usd == pytest.approx(0.003)  # $0.002 in and $0.001 out, once
    # A step may run two judges, each priced by its own meter.
    ledger.record("choose", metered(response(1_000, 100)), metered(response(2_000, 100)))
    held = json.loads(path.read_text(encoding="utf-8"))
    assert held["cap_usd"] == 60.0 and [e["step"] for e in held["entries"]] == ["pilot", "choose"]
    assert set(held["entries"][0]) == {"time", "step", "calls", "uncached_calls", "usd"}
    assert held["entries"][0]["time"].endswith("+00:00")
    assert (held["entries"][1]["calls"], held["entries"][1]["uncached_calls"]) == (2, 2)
    assert held["total_usd"] == pytest.approx(0.003 + 0.003 + 0.005)
    assert SpendLedger(path).total_usd() == pytest.approx(0.011)  # read back from the file
    assert list(path.parent.glob("*.tmp")) == []


def test_a_step_whose_estimate_would_pass_the_cap_is_refused(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    ledger.record("pilot", metered(response(150_000, 20_000)))  # $0.30 in and $0.20 out
    ledger.check("stage-a draft", 0.50)  # exactly to the cap: allowed
    with pytest.raises(
        SpendCapError,
        match=r"stage-a draft: its estimate, \$0\.51, would take the round's spend from \$0\.50 "
        r"past the \$1\.00 cap",
    ):
        ledger.check("stage-a draft", 0.51)


def test_spend_past_the_cap_is_recorded_and_refuses_every_later_step(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=0.40)
    ledger.check("pilot", 0.10)  # the estimate fits
    ledger.record("pilot", metered(response(150_000, 20_000)))  # but $0.50 was spent
    assert ledger.total_usd() == pytest.approx(0.50)
    with pytest.raises(SpendCapError, match="past the"):
        ledger.check("stage-a draft", 0.0)


def test_the_meters_of_a_step_stop_it_after_the_call_that_spends_what_the_cap_leaves(
    tmp_path: Path,
) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=2.0)
    ledger.record("pilot", metered(response(150_000, 20_000)))  # $0.50
    budget = ledger.check("stage-a draft", 0.10)  # the estimate fits, and $1.50 is left
    assert budget.usd == pytest.approx(1.50) and budget.stopped is None
    call = response(150_000, 20_000)  # $0.50 each
    llm = Replay(call, response(150_000, 20_000, cache_hit=True), call, call, call)
    # Two judges in one step draw on its one budget.
    claim, v2 = MeteredLLM(llm, PRICING, budget), MeteredLLM(llm, PRICING, budget)
    claim.create({}, salt="1")
    v2.create({}, salt="2")  # from the call cache: free
    v2.create({}, salt="3")
    with pytest.raises(
        SpendCapError,
        match=r"stage-a draft: it has spent \$1\.5000, all that the round's \$2\.00 cap left "
        r"after the \$0\.50 spent before it, so it stops here\. What it spent is recorded\.",
    ):
        claim.create({}, salt="4")  # the call that spends the rest is made, and counted
    assert claim.stopped is not None and claim.stopped == v2.stopped == budget.stopped
    with pytest.raises(SpendCapError, match="it stops here"):
        v2.create({}, salt="5")  # and no call is sent after it
    assert llm.salts == ["1", "2", "3", "4"]
    entry = ledger.record("stage-a draft (stopped early)", claim, v2)
    assert (entry.calls, entry.uncached_calls) == (4, 3) and entry.usd == pytest.approx(1.50)
    assert ledger.total_usd() == pytest.approx(2.0)  # the whole cap
    with pytest.raises(SpendCapError, match="past the"):
        ledger.check("stage-a rev1", 0.01)


def test_a_ledger_opened_with_another_cap_than_its_file_refuses(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    SpendLedger(path).record("pilot")
    with pytest.raises(ValueError, match=r"records a \$60\.00 cap"):
        SpendLedger(path, cap_usd=100.0).check("stage-a draft", 1.0)


def test_an_answer_is_estimated_at_the_pilots_cost_once_the_pilot_has_run(tmp_path: Path) -> None:
    assert cost_per_answer(tmp_path) == ESTIMATE_PER_ANSWER_USD == 0.02  # the design's estimate
    pilot = {"cost_per_answer_usd": 0.025}
    (tmp_path / "pilot.json").write_text(json.dumps(pilot), encoding="utf-8")
    assert cost_per_answer(tmp_path) == 0.025


# ---- evals judge and evals judge-probe with the claim judge ---------------------------------

QUOTE = "Bucket names must be between 3 and 63 characters long."
SPAN = "between 3 and 63 characters long"
CALL_USD = (100 * 2.0 + 20 * 10.0) / 1_000_000  # each ScriptedLLM reply: 100 tokens in, 20 out
PRICED = "{input_per_mtok: 2.0, output_per_mtok: 10.0}"


def round_ledger() -> SpendLedger:
    """The ledger the commands write: in a temporary folder in every test (conftest)."""
    assert spend.DEFAULT_PATH != DEFAULT_PATH  # never results/judge-v5/ from a test
    return SpendLedger(spend.DEFAULT_PATH)


def spent_before(usd: float) -> None:
    """The round's ledger with this much spent already, at $2 a million input tokens."""
    round_ledger().record("earlier", metered(response(round(usd * 500_000), 0)))


def judged_run(tmp_path: Path, pricing: str = PRICED) -> Path:
    """Run r1, with one cited answer on a dev case, and a claims config for it with this pricing;
    the config's path."""
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
        "judge:\n  method: claims\n  grader_name: judge-v5\n  rubric_version: v5\n"
        f"  model:\n    name: claude-sonnet-5-5\n    pricing: {pricing}\n",
        encoding="utf-8",
    )
    return config


def judge_grades(tmp_path: Path) -> dict[tuple[str, int], dict[str, float]]:
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        return store.grades_for("r1", "judge-v5")
    finally:
        store.close()


class ClaimJudge(ScriptedLLM):
    """The claim judge on whatever answer a request holds: one core claim, its words the answer's
    "between ... characters long", quoted by quote 1 and scored 90; the skeptic scores every
    flagged item 95 and finds nothing. Safe to call from several threads."""

    def __init__(self) -> None:
        super().__init__([])
        self.lock = threading.Lock()

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        message = str(request["messages"][0]["content"])
        if "Flagged items:\n" in message:
            listed = message.rsplit("Flagged items:\n", 1)[1]
            scores = [
                {"item": int(n), "score": 95, "not_a_claim": False, "why": "quoted"}
                for n in re.findall(r"(?m)^(\d+)\. ", listed)
            ]
            search = {"found": False, "answer_span": "", "role": "core", "why": "", "confidence": 0}
            reply = [tool_use("submit_skeptic", {"flagged": scores, "search": search})]
        else:
            answer = message.split("Assistant's answer:\n", 1)[1]
            found = re.search(r"between \d+ and \d+ characters long", answer)
            assert found is not None
            claim = {
                "answer_span": found.group(),
                "role": "core",
                "support": [{"quote": 1, "quote_span": SPAN}],
                "score": 90,
                "why": "quote 1",
            }
            reply = [tool_use("submit_claims", {"claims": [claim], "not_claims": []})]
        with self.lock:
            self.script = [reply]
            return super().create(request, salt=salt)


class FromCache:
    """The call cache in front of a model that has seen every request: each reply is replayed."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        reply: LLMResponse = self.llm.create(request, salt=salt)
        return reply.model_copy(update={"cache_hit": True})


def invoke(monkeypatch: pytest.MonkeyPatch, llm: Any, *args: str) -> tuple[int, str]:
    """An evals command with this model behind it; its exit code and its output on one line."""
    monkeypatch.setattr(llm_module, "AnthropicClient", lambda *a, **k: llm)
    monkeypatch.setattr(cli, "load_dotenv", lambda: False)
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, " ".join(result.output.split())


def test_evals_judge_with_the_claim_judge_records_its_spend_in_the_rounds_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = judged_run(tmp_path)
    llm = ClaimJudge()
    code, out = invoke(monkeypatch, llm, "judge", "r1", "-c", str(config))
    assert code == 0, out
    [entry] = round_ledger().entries()
    assert (entry.step, entry.calls, entry.uncached_calls) == ("evals judge r1", 2, 2)
    assert entry.usd == pytest.approx(2 * CALL_USD)
    assert "spent $0.0008: 2 of 2 calls sent to the API" in out
    assert judge_grades(tmp_path)[("t-1", 1)]["judge_supported"] == 1.0
    # Judged again, every reply from the call cache: two more calls recorded, and no money.
    code, out = invoke(monkeypatch, FromCache(llm), "judge", "r1", "--replace", "-c", str(config))
    assert code == 0, out
    assert [(e.calls, e.uncached_calls, e.usd) for e in round_ledger().entries()][1] == (2, 0, 0)


def test_evals_judge_is_refused_before_any_grade_is_deleted_when_its_estimate_passes_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = judged_run(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.add_grades("r1", "t-1", 1, [("judge-v5", Grade(metric="judge_supported", value=1.0))])
    finally:
        store.close()
    spent_before(59.99)  # the one answer's estimate, $0.02, would pass the $60 cap
    llm = ScriptedLLM([])
    code, out = invoke(monkeypatch, llm, "judge", "r1", "--replace", "-c", str(config))
    assert code == 2 and "past the $60.00 cap. Nothing was spent." in out
    assert llm.requests == [] and len(round_ledger().entries()) == 1
    assert judge_grades(tmp_path) == {("t-1", 1): {"judge_supported": 1.0}}  # as it was


def test_evals_judge_refuses_a_claim_judge_without_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM([])
    code, out = invoke(monkeypatch, llm, "judge", "r1", "-c", str(judged_run(tmp_path, "{}")))
    assert code == 2 and "no pricing" in out and "nothing was judged" in out
    assert llm.requests == [] and not spend.DEFAULT_PATH.exists()


def test_evals_judge_stopped_by_the_cap_records_what_it_spent_and_stores_no_grader_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # $0.40 a call, and $0.02 left: the estimate fits, and the first call spends what is left.
    config = judged_run(tmp_path, "{input_per_mtok: 2000.0, output_per_mtok: 10000.0}")
    spent_before(59.98)
    llm = ClaimJudge()
    code, out = invoke(monkeypatch, llm, "judge", "r1", "-c", str(config))
    assert code == 2
    assert (
        "evals judge r1: it has spent $0.4000, all that the round's $60.00 cap left after the "
        "$59.98 spent before it, so it stops here."
    ) in out
    assert "0 of the 1 answers were judged before the stop" in out
    assert len(llm.requests) == 1  # the skeptic's call was never sent
    entry = round_ledger().entries()[-1]
    assert entry.step == "evals judge r1 (stopped early)" and entry.usd == pytest.approx(0.40)
    assert judge_grades(tmp_path) == {}  # the stop is no grader_error of the answer
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        assert "judge-v5" not in store.run_info("r1")["manifest"]
    finally:
        store.close()


def test_evals_judge_with_the_verdict_judge_is_not_the_rounds_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = judged_run(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  method: claims\n  grader_name: judge-v5\n  rubric_version: v5\n", ""
        ),
        encoding="utf-8",
    )
    verdict = {"core_supported": "pass", "fully_supported": "pass", "reason": "quoted"}
    llm = ScriptedLLM(
        [
            [tool_use("submit_support_verdict", verdict)],
            [tool_use("submit_verdict", {"verdict": "pass", "reason": "answers it"})],
        ]
    )
    code, out = invoke(monkeypatch, llm, "judge", "r1", "-c", str(config))
    assert code == 0, out
    assert len(llm.requests) == 2 and not spend.DEFAULT_PATH.exists()


def test_evals_judge_probe_with_the_claim_judge_is_metered_and_scored_on_judge_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = judged_run(tmp_path)
    stored = (("judge_core_supported", 1.0), ("judge_supported", 1.0), ("judge_support_score", 0.8))
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.add_grades(
            "r1", "t-1", 1, [("judge-v5", Grade(metric=m, value=v)) for m, v in stored]
        )
    finally:
        store.close()
    monkeypatch.chdir(tmp_path)  # the probe report is written under results/: here
    llm = ClaimJudge()
    code, out = invoke(monkeypatch, llm, "judge-probe", "r1", "-c", str(config))
    assert code == 0, out
    report = json.loads(Path("results/judge-probes-r1-judge-v5.json").read_text("utf-8"))
    # Scored on judge_supported alone: the number mutant the judge passes is a miss, though its
    # support score is under 1, and the probes keep their verdict, though that score moved.
    assert {probe: row["rate"] for probe, row in report["summary"].items()} == {
        "number": 0.0,
        "padding": 1.0,
        "resample": 1.0,
    }
    [entry] = round_ledger().entries()
    assert entry.step == "evals judge-probe r1"
    assert entry.calls == entry.uncached_calls == len(llm.requests) == 6  # 3 tables, 3 skeptics
    assert entry.usd == pytest.approx(6 * CALL_USD) and "spent $0.0024" in out


def test_evals_judge_probe_with_the_claim_judge_is_refused_when_its_estimate_passes_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = judged_run(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.add_grades("r1", "t-1", 1, [("judge-v5", Grade(metric="judge_supported", value=1.0))])
    finally:
        store.close()
    spent_before(59.95)  # three variants at $0.02 would pass the cap
    monkeypatch.chdir(tmp_path)
    llm = ScriptedLLM([text("never asked")])
    code, out = invoke(monkeypatch, llm, "judge-probe", "r1", "-c", str(config))
    assert code == 2 and "evals judge-probe r1: its estimate, $0.06" in out
    assert llm.requests == [] and not Path("results").exists()
