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
    assert index == {make_case().gold.evidence_urls[0]: [(good.as_posix(), "d-1")]}


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


def test_paraphrase_consistency_counts_groups_that_agree() -> None:
    from agent_evals.report import paraphrase_consistency

    correct = {"a": [1.0], "a1": [1.0], "a2": [1.0], "b": [1.0], "b1": [0.0], "c": [0.0]}
    groups = {"a": ["a1", "a2"], "b": ["b1"], "c": ["c1"]}  # c1 was not in the run
    est = paraphrase_consistency(correct, groups)
    assert est is not None and est.n == 2 and est.mean == 0.5  # a agrees, b disagrees, c skipped
    assert paraphrase_consistency(correct, {}) is None


def test_summary_has_area_breakdown_cost_per_success_and_latency(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [
        make_case(id="s1", area="storage"),
        make_case(id="s2", area="storage"),
        make_case(id="c1", area="compute"),
    ]
    suite = tmp_path / "suite.jsonl"
    suite.write_text("\n".join(c.model_dump_json(exclude_none=True) for c in cases) + "\n")
    run = run_suite(
        agent=FakeAgent({"s1": {1}, "c1": {1}}),
        cases=cases,
        graders=[AnswerGrader()],
        store=store,
        suite_name="t",
        attempts=1,
        workers=1,
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=1.0),
        budget_usd=None,
        manifest={"suite": {"path": str(suite)}},
        show_progress=False,
    )
    s = summarize(store, run)
    assert set(s.by_area) == {"storage", "compute"}
    assert s.by_area["storage"].mean == 0.5 and s.by_area["compute"].mean == 1.0
    # FakeAgent reports 1,000,000 input tokens per attempt at $1/MTok: $3 total over 2 successes.
    assert s.cost_per_success == 1.5
    assert s.latency_p50_ms is not None and s.latency_p95_ms is not None
    rec = store.attempt(run, "s2", 1)
    assert rec is not None and rec["output"]["abstained"] is True and rec["grades"]
    assert store.attempt(run, "nope", 1) is None
    store.close()


def test_flip_rate_and_pass_at_k_need_repeated_attempts(tmp_path: Path) -> None:
    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id="c0"), make_case(id="c1"), make_case(id="c2")]
    two = _run(store, FakeAgent({"c0": {1, 2}, "c1": {1}}), cases, attempts=2)
    s = summarize(store, two)
    assert s.flip_rate is not None
    assert s.flip_rate.n == 3 and s.flip_rate.mean == pytest.approx(1 / 3)  # only c1 flipped
    # c0 passes both attempts, c1 one of two, c2 none: at-least-one is 1, 1, 0; all-of is 1, 0, 0.
    assert s.pass_at_k[2] == pytest.approx(2 / 3) and s.pass_hat_k[2] == pytest.approx(1 / 3)
    assert s.pass_at_k[1] == s.pass_hat_k[1] == s.metrics["correct"].mean
    one = _run(store, FakeAgent({"c0": {1}}), cases, attempts=1)
    s1 = summarize(store, one)
    assert s1.flip_rate is None and s1.pass_at_k == {1: s1.pass_hat_k[1]}
    store.close()


def test_gate_passes_only_when_target_improves_and_nothing_regresses(tmp_path: Path) -> None:
    from agent_evals.report import gate

    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id=f"s{i}", area="storage") for i in range(6)]
    cases += [make_case(id=f"c{i}", area="compute") for i in range(6)]
    # Champion: storage all pass, compute all fail. Challenger: everything passes.
    champ = _run(store, FakeAgent({f"s{i}": {1} for i in range(6)}), cases, attempts=1)
    better = _run(store, FakeAgent({c.id: {1} for c in cases}), cases, attempts=1)
    g = gate(store, champ, better, cases, target="correct")
    assert g.target.verdict == "improved" and g.passed
    assert not g.regressed_categories and "area=compute" not in g.regressed_categories
    # A challenger that fixes compute but breaks storage: the target still improves on average,
    # yet the storage category's paired interval excludes zero, so the gate fails.
    mixed = _run(store, FakeAgent({f"c{i}": {1} for i in range(6)}), cases, attempts=1)
    g2 = gate(store, champ, mixed, cases, target="correct")
    assert g2.target.verdict == "no detectable change" or not g2.passed
    assert "area=storage" in g2.regressed_categories
    assert g2.skipped  # FakeAgent runs carry no citation metrics, so those are skipped, not failed
    store.close()


def test_regrade_rescores_stored_answers_against_changed_cases(tmp_path: Path) -> None:
    from agent_evals.harness.runner import regrade_run
    from agent_evals.types import Gold, Grade

    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id="a"), make_case(id="b")]
    run = _run(store, FakeAgent({"a": {1}, "b": {1}}), cases, attempts=1)  # both answer "63"
    assert summarize(store, run).metrics["correct"].mean == 1.0
    store.add_grades(run, "a", 1, [("judge", Grade(metric="judge_supported", value=1.0))])
    # The keyword list for "b" is tightened after the run: "63" alone no longer passes.
    tightened = Gold(answer="", must_include=[["sixty-three"]])
    changed = [cases[0], cases[1].model_copy(update={"gold": tightened})]
    assert regrade_run(store, run, changed, [AnswerGrader()]) == 2
    s = summarize(store, run)
    assert s.metrics["correct"].mean == 0.5
    assert s.metrics["judge_supported"].mean == 1.0  # the judge's grades were left alone
    assert store.run_info(run)["manifest"]["regraded"]["graders"] == {"answer_keywords": "2"}
    store.close()


def test_store_post_hoc_grades_and_labels(tmp_path: Path) -> None:
    from agent_evals.types import Grade

    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id="a"), make_case(id="b"), make_case(id="boom")]
    run = _run(store, FakeAgent({"a": {1}}), cases, attempts=1)
    answered = store.answered_attempts(run)
    assert [x["case_id"] for x in answered] == ["a"]  # b abstained, boom errored
    assert store.grade_count(run, "judge") == 0
    grade = Grade(metric="judge_supported", value=1.0, passed=True)
    store.add_grades(run, "a", 1, [("judge", grade)])
    assert store.grade_count(run, "judge") == 1
    assert store.grades_for(run, "judge") == {("a", 1): {"judge_supported": 1.0}}
    assert store.grade_reasons(run, "judge") == {("a", 1): {"judge_supported": (1.0, "")}}
    assert summarize(store, run).metrics["judge_supported"].mean == 1.0
    store.delete_grades(run, "judge", [("zzz", 9)])  # keyed delete touches only those keys
    assert store.grade_count(run, "judge") == 1
    store.delete_grades(run, "judge", [("a", 1)])
    assert store.grades_for(run, "judge") == {}
    store.add_grades(run, "a", 1, [("judge", grade)])
    store.delete_grades(run, "judge")
    assert store.grades_for(run, "judge") == {}
    store.record_label(
        run, "a", 1, "aleena", supported=True, responsive=False, note="", label_split="judge-dev"
    )
    store.record_label(
        run,
        "a",
        1,
        "aleena",
        supported=False,
        responsive=False,
        note="x",
        label_split="judge-dev",
        core_supported=True,
    )
    labels = store.labels(run, "aleena")
    assert len(labels) == 1 and labels[0]["supported"] is False and labels[0]["note"] == "x"
    assert labels[0]["core_supported"] is True  # lenient question kept beside the strict one
    store.annotate_run(run, "judge", {"version": "abc"})
    assert store.run_info(run)["manifest"]["judge"] == {"version": "abc"}
    store.close()


def test_two_judges_on_one_run_are_never_averaged(tmp_path: Path) -> None:
    from agent_evals.report import gate
    from agent_evals.types import Grade

    store = ResultsStore(tmp_path / "e.duckdb")
    cases = [make_case(id="a"), make_case(id="b")]
    one = _run(store, FakeAgent({"a": {1}, "b": {1}}), cases, attempts=1)
    two = _run(store, FakeAgent({"a": {1}, "b": {1}}), cases, attempts=1)
    for run in (one, two):
        for cid in ("a", "b"):
            store.add_grades(run, cid, 1, [("judge", Grade(metric="judge_supported", value=1.0))])
            v4 = Grade(metric="judge_supported", value=0.0)
            store.add_grades(run, cid, 1, [("judge-v4", v4)])
    store.add_grades(one, "a", 1, [("judge-v4", Grade(metric="grader_error", value=1.0))])
    assert store.judge_graders(one) == ["judge", "judge-v4"]
    s = summarize(store, one)
    assert s.metrics["judge_supported"].mean == 1.0  # the validated judge alone, not 0.5
    assert "grader_error" not in s.metrics  # the candidate's error is not the run's
    assert summarize(store, one, judge="judge-v4").metrics["judge_supported"].mean == 0.0
    assert compare(store, one, two, "judge_supported").baseline == 1.0
    assert compare(store, one, two, "judge_supported", judge="judge-v4").candidate == 0.0
    assert gate(store, one, two, cases, judge="judge-v4").target.baseline == 0.0
    assert summarize(store, one).metrics["correct"].mean == 1.0  # code graders unaffected
    store.close()
