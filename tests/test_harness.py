from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_evals.config import Pricing
from agent_evals.graders.deterministic import AnswerGrader
from agent_evals.harness.cache import CallCache
from agent_evals.harness.runner import run_suite
from agent_evals.report import compare, summarize
from agent_evals.store.db import ResultsStore
from agent_evals.suites import SuiteError, evidence_index, load_suite
from agent_evals.types import AgentResult, Case, RunContext, Usage
from conftest import make_case


def test_cache_roundtrip_and_salt(tmp_path: Path) -> None:
    cache = CallCache(tmp_path)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    k1, k2 = CallCache.key(req, "case#1"), CallCache.key(req, "case#2")
    assert k1 != k2  # separate attempts are separate samples
    assert CallCache.key(dict(reversed(list(req.items()))), "case#1") == k1  # key order ignored
    cache.put(k1, {"content": [1]})
    assert cache.get(k1) == {"content": [1]}
    assert cache.get(k2) is None


class FakeAgent:
    """Passes a case on attempts listed in `passes`, abstains otherwise; raises on 'boom'."""

    name = "fake"

    def __init__(self, passes: dict[str, set[int]]) -> None:
        self.passes = passes

    @property
    def version(self) -> str:
        return "v-test"

    def describe(self) -> dict[str, Any]:
        return {}

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        if case.id == "boom":
            raise RuntimeError("kaboom")
        ok = ctx.attempt in self.passes.get(case.id, set())
        return AgentResult(
            output={
                "answer": "63" if ok else "",
                "citations": [],
                "abstained": not ok,
                "confidence": 0.5,
            },
            usage=Usage(input_tokens=1_000_000, output_tokens=0, model_calls=1),
        )


def _run(
    store: ResultsStore,
    agent: FakeAgent,
    cases: list[Case],
    attempts: int,
    budget: float | None = None,
    manifest: dict[str, Any] | None = None,
) -> str:
    return run_suite(
        agent=agent,
        cases=cases,
        graders=[AnswerGrader()],
        store=store,
        suite_name="t",
        attempts=attempts,
        workers=2,
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=1.0),
        budget_usd=budget,
        manifest=manifest or {"test": True},
        show_progress=False,
    )


def test_run_report_and_compare(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id=f"c{i}") for i in range(4)] + [make_case(id="boom")]

    base = _run(store, FakeAgent({"c0": {1, 2}, "c1": {1}}), cases, attempts=2)
    s = summarize(store, base)
    assert s.info["status"] == "complete"
    assert s.totals["attempts"] == 10 and s.totals["errors"] == 2
    # Per-case means: c0=1, c1=.5, others 0 -> mean .3
    assert abs(s.metrics["correct"].mean - 0.3) < 1e-9
    assert s.metrics["correct"].method == "bootstrap"  # 2 attempts: per-case means can be 0.5
    assert s.pass_hat_k[2] == 0.2  # only c0 passed both attempts
    assert any(f[0] == "boom" and "kaboom" in f[2] for f in s.failures)

    cand = _run(store, FakeAgent({c.id: {1, 2} for c in cases[:4]}), cases, attempts=2)
    cmp = compare(store, base, cand)
    assert cmp.n_cases == 5
    assert sorted(cmp.only_candidate_passed) == ["c1", "c2", "c3"]
    assert cmp.diff.mean > 0 and cmp.verdict == "improved"
    # A lower-is-better metric must read the same improvement, not report a regression.
    wa = compare(store, base, cand, metric="wrong_abstention")
    assert wa.lower_is_better and wa.n_cases == 4  # "boom" errored, so it has no abstention grade
    assert sorted(wa.only_candidate_passed) == ["c1", "c2", "c3"]
    assert wa.only_baseline_passed == []
    assert wa.diff.mean < 0 and wa.verdict == "improved"
    with pytest.raises(ValueError, match="no cases graded on 'nope'"):
        compare(store, base, cand, metric="nope")
    assert cmp.same_snapshot is None  # these manifests record no fingerprint
    store.close()


def test_compare_notices_a_different_snapshot(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id=f"c{i}") for i in range(3)]
    a = _run(store, FakeAgent({}), cases, attempts=1, manifest={"corpus_fingerprint": "aaa"})
    b = _run(store, FakeAgent({}), cases, attempts=1, manifest={"corpus_fingerprint": "bbb"})
    c = _run(store, FakeAgent({}), cases, attempts=1, manifest={"corpus_fingerprint": "bbb"})
    assert compare(store, a, b).same_snapshot is False
    assert compare(store, b, c).same_snapshot is True
    store.close()


def test_evidence_index_maps_pages_to_cases(tmp_path: Path) -> None:
    good = tmp_path / "dev.jsonl"
    good.write_text(make_case(id="d-1").model_dump_json() + "\n")
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n")
    index = evidence_index([good, broken])
    assert index == {make_case().gold.evidence[0]: [(good.as_posix(), "d-1")]}


def test_single_attempt_pass_rates_get_wilson_intervals(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id=f"c{i}") for i in range(4)]
    run = _run(store, FakeAgent({c.id: {1} for c in cases}), cases, attempts=1)
    est = summarize(store, run).metrics["correct"]
    assert est.method == "wilson" and est.mean == 1.0
    assert 0.4 < est.low < 1.0 == est.high  # 4/4 is not certainty; the bootstrap said [1, 1]
    store.close()


def test_budget_stops_the_run(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id=f"c{i}") for i in range(50)]
    run_id = _run(store, FakeAgent({}), cases, attempts=1, budget=2.5)
    info = store.run_info(run_id)
    assert info["status"] == "budget_exceeded"
    assert store.totals(run_id)["attempts"] < 50
    store.close()


def test_suite_validation(tmp_path: Path) -> None:
    good = make_case().model_dump_json()
    bad = make_case(id="x", gold={"answer": "a"}).model_dump_json()  # answerable, no keywords
    f = tmp_path / "s.jsonl"
    f.write_text(f"{good}\n{good}\n{bad}\n")
    try:
        load_suite(f)
    except SuiteError as exc:
        msg = str(exc)
        assert "duplicate id" in msg and "no must_include" in msg
    else:
        raise AssertionError("expected SuiteError")


def test_repo_smoke_suite_is_valid() -> None:
    cases = load_suite(Path(__file__).parents[1] / "suites/docs/smoke.jsonl")
    assert len(cases) >= 5
