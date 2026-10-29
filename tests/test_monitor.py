"""The online monitor's rule (phase 7 part 3) on synthetic days, and its reading of a real store."""

from __future__ import annotations

from dataclasses import replace
from math import log
from pathlib import Path
from typing import Any

import pytest

from agent_evals import online
from agent_evals.harness.manifest import file_sha256
from agent_evals.monitor import (
    ALERT_FAMILY,
    CANNOT_ALERT,
    DayValues,
    check_day,
    day_run,
    day_runs,
    day_values,
    leave_one_day_out,
    load_day,
    not_cleared,
    verdict_code,
)
from agent_evals.online import ONLINE_JUDGE_VERSION, online_case
from agent_evals.simulate import TrafficQuestion
from agent_evals.stats.monitor import detectable_rate_rise
from agent_evals.store.db import ResultsStore
from agent_evals.suites import write_suite
from agent_evals.types import AgentResult, Grade, Usage
from conftest import LAMBDA_URL


def _ones(n: int, k: int) -> list[float]:
    """n values with exactly k bad outcomes, spread evenly."""
    return [1.0 if (i * k) // n != ((i + 1) * k) // n else 0.0 for i in range(n)]


def _day(run_id: str, **override: Any) -> DayValues:
    values: dict[str, list[float]] = {
        "judge_supported": _ones(80, 24),
        "uncited_answer": _ones(80, 8),
        "in_scope_abstention": _ones(80, 4),
        "error": _ones(100, 1),
        "model_calls": [3.0, 4.0, 5.0, 4.0] * 10,  # small, to keep the 20,000 draws quick
        "latency": [8.9, 9.0, 9.1, 9.0] * 10,
        "tokens": [9.4, 9.5, 9.6, 9.5] * 10,
    }
    values.update(override.pop("values", {}))
    day = DayValues(
        run_id=run_id,
        values=values,
        fabricated=override.pop("fabricated", _ones(80, 1)),
        cache_hits=override.pop("cache_hits", 0),
        answer_words=override.pop("answer_words", [40] * 80),
        source_named=override.pop("source_named", [1, 1, 1, 0] * 20),
        reported={"median_answer_words": 40.0, "source_named_share": 0.75},
    )
    day.reported.update(override.pop("reported", {}))
    assert not override
    return day


REFERENCE = [_day(f"ref-{d}") for d in range(1, 6)]


def _row(report: dict[str, Any], metric: str) -> dict[str, Any]:
    return next(r for r in report["metrics"] if r["metric"] == metric)


def test_a_quiet_day_raises_nothing_and_the_judge_waits_for_its_labels() -> None:
    report = check_day(_day("d6"), REFERENCE, judge_enabled=False)
    assert report["alerts"] == [] and report["family_size"] == len(ALERT_FAMILY) - 1
    assert _row(report, "judge_supported")["status"].startswith("pending")
    assert _row(report, "uncited_answer")["detectable"] is not None  # its blind spot, published
    assert check_day(_day("d6"), REFERENCE, judge_enabled=True)["family_size"] == len(ALERT_FAMILY)


def test_a_real_rise_alerts_and_a_small_significant_one_stays_under_its_floor() -> None:
    worse = check_day(
        _day("d7", values={"uncited_answer": _ones(80, 30)}), REFERENCE, judge_enabled=False
    )
    assert worse["alerts"] == ["uncited_answer"]
    big = [_day(f"r{d}", values={"uncited_answer": _ones(400, 40)}) for d in range(5)]
    small = _day("d8", values={"uncited_answer": _ones(400, 58)})  # +4.5 points, under 5
    row = _row(check_day(small, big, judge_enabled=False), "uncited_answer")
    assert row["holm_rejected"] and row["status"] == "ok"
    assert row["effect"] == pytest.approx(0.045)


def test_means_alert_on_a_rise_past_the_floor_and_latency_needs_uncached_attempts() -> None:
    slow = _day("d9", values={"latency": [x + log(1.6) for x in [8.9, 9.0, 9.1, 9.0] * 10]})
    assert "latency" in check_day(slow, REFERENCE, judge_enabled=False)["alerts"]
    cached = _day("d9", values=slow.values, cache_hits=3)
    row = _row(check_day(cached, REFERENCE, judge_enabled=False), "latency")
    assert row["status"].startswith("refused") and row["p"] is None


def test_fabricated_citations_have_their_own_rule_outside_holm() -> None:
    report = check_day(_day("d10", fabricated=_ones(80, 12)), REFERENCE, judge_enabled=False)
    assert report["fabricated_citation"]["alert"] and report["alerts"] == ["fabricated_citation"]


def test_a_style_shift_makes_the_judge_untrusted_so_its_quiet_does_not_clear_the_day() -> None:
    terse = _day("d11", answer_words=[20] * 80, reported={"median_answer_words": 20.0})
    report = check_day(terse, REFERENCE, judge_enabled=True)
    assert (
        not report["judge"]["trusted"] and report["judge"]["shifts"]["median_answer_words"] == 0.5
    )
    assert "untrusted" in _row(report, "judge_supported")["status"]


def test_leave_one_day_out_is_quiet_on_like_days_and_loud_on_an_odd_one() -> None:
    assert all(r["alerts"] == [] for r in leave_one_day_out(REFERENCE, judge_enabled=False))
    odd = [*REFERENCE[:4], _day("odd", values={"uncited_answer": _ones(80, 30)})]
    assert any(r["alerts"] for r in leave_one_day_out(odd, judge_enabled=False))


def _traffic() -> list[TrafficQuestion]:
    base = {
        "day": 1,
        "persona": "on-call",
        "question": "How much memory?",
        "source_url": LAMBDA_URL,
    }
    return [
        TrafficQuestion(id="q-look", intent="lookup", type="lookup", **base),
        TrafficQuestion(id="q-cmp", intent="comparison", type="multi_step", **base),
        TrafficQuestion(
            id="q-out",
            intent="outside_snapshot",
            type="unanswerable",
            **(base | {"source_url": None}),
            service="Braket",
        ),
    ]


def _attempt(
    store: ResultsStore, run: str, case: str, result: AgentResult, grades: dict[str, list[Grade]]
) -> None:
    store.record_attempt(
        run, case, 1, result, 0.0, [(g, x) for g, xs in grades.items() for x in xs]
    )


def test_day_values_reads_the_plan_metrics_from_a_store(tmp_path: Path) -> None:
    traffic = _traffic()
    cases = {q.id: online_case(q) for q in traffic}
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.start_run(
            "r",
            agent="docs",
            agent_version="v",
            suite="day-01",
            attempts=1,
            manifest={},
            online=True,
        )
        usage = Usage(input_tokens=900, output_tokens=100, model_calls=3, latency_ms=2000.0)
        cited = AgentResult(
            output={
                "answer": "Up to 10,240 MB of memory.",
                "citations": [{"url": LAMBDA_URL, "quote": "x"}],
                "abstained": False,
            },
            usage=usage,
        )
        _attempt(
            store,
            "r",
            "q-look",
            cited,
            {
                "answer_keywords": [Grade(metric="abstained", value=0.0)],
                "citations": [
                    Grade(metric="uncited_answer", value=0.0),
                    Grade(metric="fabricated_citation", value=0.0),
                ],
                "trajectory": [Grade(metric="model_calls", value=3.0)],
                "judge": [
                    Grade(metric="judge_supported", value=0.0),
                    Grade(metric="judge_core_supported", value=1.0),
                ],
            },
        )
        declined = AgentResult(
            output={"answer": "", "citations": [], "abstained": True}, usage=usage
        )
        _attempt(
            store,
            "r",
            "q-cmp",
            declined,
            {"answer_keywords": [Grade(metric="abstained", value=1.0)]},
        )
        _attempt(store, "r", "q-out", AgentResult(error="boom", usage=usage), {})
        v = day_values(store, "r", cases, {q.id: q for q in traffic})
    finally:
        store.close()
    # attempts come back in case-id order: q-cmp, q-look, q-out
    assert v.values["error"] == [0.0, 0.0, 1.0]
    assert v.values["judge_supported"] == [1.0]  # not supported: the bad outcome
    assert v.values["in_scope_abstention"] == [1.0, 0.0]  # the comparison declined
    assert v.values["uncited_answer"] == [0.0] and v.values["tokens"] == [pytest.approx(log(1000))]
    assert len(v.values["latency"]) == 3 and v.source_named == [1] and v.answer_words == [6]
    assert v.reported["judge_supported_over_all_in_scope"] == 0.0  # the comparison declined


def _online_run(store: ResultsStore, run_id: str, cases: Path, traffic: Path, **role: Any) -> None:
    """A complete online run of day 1's three questions: the lookup cited and judged."""
    manifest = {"suite": {"path": str(cases), "sha256": file_sha256(cases)}}
    store.start_run(
        run_id,
        agent="docs",
        agent_version="v",
        suite="day-01",
        attempts=1,
        manifest=manifest,
        online=True,
    )
    store.annotate_run(
        run_id,
        "online",
        {"day": 1, "champion": True, "traffic_sha256": file_sha256(traffic)} | role,
    )
    store.annotate_run(run_id, "judge", {"version": ONLINE_JUDGE_VERSION})
    usage = Usage(input_tokens=900, output_tokens=100, model_calls=3, latency_ms=2000.0)
    cited = {"answer": "Up to 10,240 MB.", "citations": [{"url": LAMBDA_URL, "quote": "x"}]}
    _attempt(
        store,
        run_id,
        "q-look",
        AgentResult(output=cited | {"abstained": False}, usage=usage),
        {"judge": [Grade(metric="judge_supported", value=1.0)]},
    )
    declined = {"answer": "", "citations": [], "abstained": True}
    for case in ("q-cmp", "q-out"):
        _attempt(store, run_id, case, AgentResult(output=declined, usage=usage), {})
    store.finish_run(run_id, "complete")


def _files(tmp_path: Path) -> tuple[Path, Path]:
    """Day 1's traffic file and the cases file built from it."""
    traffic = tmp_path / "day-01.jsonl"
    traffic.write_text("".join(q.model_dump_json() + "\n" for q in _traffic()), "utf-8")
    cases = tmp_path / "online-day-01.jsonl"
    write_suite(cases, [online_case(q) for q in _traffic()])
    return traffic, cases


def test_the_day_run_is_the_one_complete_served_run(tmp_path: Path) -> None:
    traffic, cases = _files(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        _online_run(store, "old", cases, traffic)
        _online_run(store, "challenger", cases, traffic, champion=False)
        _online_run(store, "shadow", cases, traffic, shadow=True)
        assert day_runs(store, 1) == ["old", "challenger"]  # a shadow is never the day's run
        assert day_runs(store, 1, champion_only=True) == ["old"] and day_run(store, 2) is None
        with pytest.raises(ValueError, match="2 complete online runs"):
            day_run(store, 1)  # which one counts is a decision, not a default
        store.finish_run("challenger", "failed")
        assert day_run(store, 1) == "old"
    finally:
        store.close()


def test_load_day_refuses_a_run_that_would_read_as_a_quiet_day(tmp_path: Path) -> None:
    traffic, cases = _files(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        _online_run(store, "ok", cases, traffic)
        assert load_day(store, "ok", tmp_path).values["judge_supported"] == [0.0]
        _online_run(store, "unjudged", cases, traffic)
        store.delete_grades("unjudged", "judge")
        with pytest.raises(ValueError, match="no judge verdict"):
            load_day(store, "unjudged", tmp_path)
        _online_run(store, "other-judge", cases, traffic)
        store.annotate_run("other-judge", "judge", {"version": "0123456789ab"})
        with pytest.raises(ValueError, match="pinned judge"):
            load_day(store, "other-judge", tmp_path)
        _online_run(store, "running", cases, traffic)
        store.finish_run("running", "running")
        with pytest.raises(ValueError, match="not complete"):
            load_day(store, "running", tmp_path)
        traffic.write_text(traffic.read_text("utf-8").replace("memory", "storage"), "utf-8")
        with pytest.raises(ValueError, match="traffic file"):
            load_day(store, "ok", tmp_path)  # questions changed under the same ids
        write_suite(cases, [online_case(q) for q in _traffic()[:2]])
        with pytest.raises(ValueError, match="changed since it ran"):
            load_day(store, "ok", tmp_path)
    finally:
        store.close()


def test_a_nan_latency_is_skipped_not_tested(tmp_path: Path) -> None:
    traffic = _traffic()
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.start_run(
            "r", agent="docs", agent_version="v", suite="x", attempts=1, manifest={}, online=True
        )
        for q, latency in zip(traffic, (float("nan"), 0.0, 1500.0), strict=True):
            usage = Usage(input_tokens=10, output_tokens=1, model_calls=1, latency_ms=latency)
            store.record_attempt("r", q.id, 1, AgentResult(output={}, usage=usage), 0.0, [])
        v = day_values(store, "r", {q.id: online_case(q) for q in traffic}, {})
    finally:
        store.close()
    assert v.values["latency"] == [pytest.approx(log(1500.0))]
    assert v.reported["judge_supported_over_all_in_scope"] is None  # nothing judged: unknown


def test_in_scope_support_counts_lookups_and_comparisons_not_false_premises(
    tmp_path: Path,
) -> None:
    traffic = [
        *_traffic(),
        TrafficQuestion(
            id="q-prem",
            day=1,
            persona="on-call",
            intent="false_premise",
            type="false_premise",
            question="Why does Lambda cap memory at 1 GB?",
            source_url=LAMBDA_URL,
            contradicts="You can configure memory between 128 MB and 10,240 MB.",
        ),
    ]
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        store.start_run(
            "r", agent="docs", agent_version="v", suite="x", attempts=1, manifest={}, online=True
        )
        output = {"answer": "a", "citations": [{"url": LAMBDA_URL, "quote": "x"}]}
        supported = [("judge", Grade(metric="judge_supported", value=1.0))]
        for q in traffic:
            result = AgentResult(output=output | {"abstained": False})
            store.record_attempt("r", q.id, 1, result, 0.0, supported if q.id == "q-look" else [])
        v = day_values(store, "r", {q.id: online_case(q) for q in traffic}, {})
    finally:
        store.close()
    # the lookup supported, the comparison not; the false premise and outside question left out
    assert v.reported["judge_supported_over_all_in_scope"] == 0.5


def test_every_metric_publishes_its_blind_spot_at_holms_first_step() -> None:
    report = check_day(_day("d12"), REFERENCE, judge_enabled=False)
    levels = {m["metric"]: m["detectable_alpha"] for m in report["metrics"]}
    assert levels.pop("judge_supported") == pytest.approx(0.05 / 7)  # pending: would join 6
    assert all(level == pytest.approx(0.05 / 6) for level in levels.values())
    assert all(m["detectable"] is not None for m in report["metrics"])
    cached = check_day(_day("d13", cache_hits=2), REFERENCE, judge_enabled=False)
    assert _row(cached, "latency")["detectable_alpha"] == pytest.approx(0.05 / 6)  # 5 + it
    assert _row(cached, "uncited_answer")["detectable_alpha"] == pytest.approx(0.05 / 5)
    assert _row(cached, "latency")["detectable"] is not None  # refused, yet its blind spot shown
    rf = [x for r in REFERENCE for x in r.fabricated]
    assert report["fabricated_citation"]["detectable"] == detectable_rate_rise(
        bad_ref=round(sum(rf)), n_ref=len(rf), n_day=80, alpha=0.01, strict=True
    )
    # a judge row of 4 answers: even 4 of 4 unsupported cannot pass Fisher at 0.05/7
    tiny = check_day(
        _day("d14", values={"judge_supported": [1.0] * 4}), REFERENCE, judge_enabled=True
    )
    row = _row(tiny, "judge_supported")
    assert row["detectable"] is None and row["detectable_note"] == CANNOT_ALERT


def test_an_untested_metric_or_an_untrusted_judge_does_not_clear_a_quiet_day() -> None:
    cached = check_day(_day("d8", cache_hits=3), REFERENCE, judge_enabled=False)
    assert cached["alerts"] == [] and cached["untested"] == ["latency"]
    assert cached["family_size"] == 5  # Holm ran over the 5 metrics tested, and says so
    assert not_cleared(cached) == ["latency (refused: 3 attempts hit the local call cache)"]
    terse = _day("d9", answer_words=[20] * 80, reported={"median_answer_words": 20.0})
    assert not_cleared(check_day(terse, REFERENCE, judge_enabled=False)) == []  # judge not in
    assert not_cleared(check_day(terse, REFERENCE, judge_enabled=True)) == [
        "judge_supported (the judge is untrusted today)"
    ]
    assert not_cleared(check_day(_day("d10"), REFERENCE, judge_enabled=True)) == []


CLAIM_PIN = "abcdefabcdef"  # a made-up pin for the claim judge, judge-v5, in these tests
PINS = {"judge": ONLINE_JUDGE_VERSION, "judge-v5": CLAIM_PIN}


def _claim_judged_run(
    store: ResultsStore, run_id: str, cases: Path, traffic: Path, grades: dict[str, list[Grade]]
) -> None:
    """Day 1's run with the lookup and the comparison answered and cited, judged by the validated
    judge (both supported) and by judge-v5 at its pin, with these grades per case. The outside
    question is answered without a citation, so no judge grades it."""
    manifest = {"suite": {"path": str(cases), "sha256": file_sha256(cases)}}
    store.start_run(
        run_id,
        agent="docs",
        agent_version="v",
        suite="x",
        attempts=1,
        manifest=manifest,
        online=True,
    )
    role = {"day": 1, "champion": True, "traffic_sha256": file_sha256(traffic)}
    store.annotate_run(run_id, "online", role)
    store.annotate_run(run_id, "judge", {"version": ONLINE_JUDGE_VERSION})
    store.annotate_run(run_id, "judge-v5", {"version": CLAIM_PIN, "method": "claims"})
    usage = Usage(input_tokens=900, output_tokens=100, model_calls=3, latency_ms=2000.0)
    cited = {"answer": "Up to 10,240 MB.", "citations": [{"url": LAMBDA_URL, "quote": "x"}]}
    for case in ("q-look", "q-cmp"):
        result = AgentResult(output=cited | {"abstained": False}, usage=usage)
        _attempt(
            store,
            run_id,
            case,
            result,
            {
                "answer_keywords": [Grade(metric="abstained", value=0.0)],
                "judge": [Grade(metric="judge_supported", value=1.0)],
                "judge-v5": grades.get(case, []),
            },
        )
    uncited = {"answer": "Braket has no such limit.", "citations": [], "abstained": False}
    _attempt(
        store,
        run_id,
        "q-out",
        AgentResult(output=uncited, usage=usage),
        {"answer_keywords": [Grade(metric="abstained", value=0.0)]},
    )
    store.finish_run(run_id, "complete")


def test_load_day_reads_the_judge_it_is_asked_for_and_only_at_that_judges_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traffic, cases = _files(tmp_path)
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        fails = {c: [Grade(metric="judge_supported", value=0.0)] for c in ("q-look", "q-cmp")}
        _claim_judged_run(store, "r", cases, traffic, fails)
        with pytest.raises(ValueError, match="judge-v5 has no online pin"):
            load_day(store, "r", tmp_path, "judge-v5")  # graded, noted, but never pinned
        monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", PINS)
        assert load_day(store, "r", tmp_path).values["judge_supported"] == [0.0, 0.0]  # v2's
        assert load_day(store, "r", tmp_path, "judge-v5").values["judge_supported"] == [1.0, 1.0]
        store.annotate_run("r", "judge-v5", {"version": "0123456789ab"})  # judged at another one
        with pytest.raises(ValueError, match=r"pinned judge abcdefabcdef \(judge-v5\)"):
            load_day(store, "r", tmp_path, "judge-v5")
        assert load_day(store, "r", tmp_path).values["judge_supported"] == [0.0, 0.0]  # unmoved
    finally:
        store.close()


def test_a_judge_error_is_no_verdict_it_is_counted_and_a_day_with_one_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(online, "ONLINE_JUDGE_PINS", PINS)
    traffic, cases = _files(tmp_path)
    passed = Grade(metric="judge_supported", value=1.0)
    error = Grade(metric="judge_error", value=1.0, passed=False, reason="failed twice")
    store = ResultsStore(tmp_path / "evals.duckdb")
    try:
        _claim_judged_run(store, "r", cases, traffic, {"q-look": [passed], "q-cmp": [error]})
        day = load_day(store, "r", tmp_path, "judge-v5")  # accepted in place of a verdict
        assert day.values["judge_supported"] == [0.0]  # the lookup's verdict alone: no fail added
        assert day.judge_errors == 1 and day.cited_answers == 2
        assert day.reported["judge_supported_over_all_in_scope"] == 1.0  # nor an unsupported one
        assert load_day(store, "r", tmp_path).judge_errors == 0  # v2's grades hold none
        scored = Grade(metric="judge_support_score", value=0.5)
        _claim_judged_run(
            store, "r2", cases, traffic, {"q-look": [passed], "q-cmp": [error, scored]}
        )
        with pytest.raises(ValueError, match="no judge verdict"):  # not its only judge grade
            load_day(store, "r2", tmp_path, "judge-v5")
        _claim_judged_run(
            store, "r3", cases, traffic, {"q-look": [passed], "q-cmp": [error, passed]}
        )
        with pytest.raises(ValueError, match="both a judge verdict and a judge error"):
            load_day(store, "r3", tmp_path, "judge-v5")
    finally:
        store.close()


def test_judge_errors_past_two_percent_of_the_cited_answers_make_the_judge_untrusted() -> None:
    at_bar = replace(_day("d15"), judge_errors=2, cited_answers=100)
    report = check_day(at_bar, REFERENCE, judge_enabled=True)
    assert report["judge"]["trusted"] and not_cleared(report) == []  # 2% is not past 2%
    counted = {k: report["judge"][k] for k in ("errors", "cited", "error_share")}
    assert counted == {"errors": 2, "cited": 100, "error_share": 0.02}
    past = replace(at_bar, judge_errors=3)
    report = check_day(past, REFERENCE, judge_enabled=True)
    assert not report["judge"]["trusted"] and report["judge"]["error_share"] == 0.03
    assert "untrusted" in _row(report, "judge_supported")["status"]
    assert not_cleared(report) == ["judge_supported (the judge is untrusted today)"]
    assert report["alerts"] == [] and verdict_code(report) == 3
    out = check_day(past, REFERENCE, judge_enabled=False)  # out of the family: counted all the same
    assert out["judge"]["errors"] == 3 and not out["judge"]["trusted"] and not_cleared(out) == []
    clean = check_day(_day("d16"), REFERENCE, judge_enabled=True)  # none, as for the v2 judge
    assert clean["judge"]["trusted"] and clean["judge"]["errors"] == 0
    assert clean["judge"]["error_share"] is None and verdict_code(clean) == 0


def test_the_monitor_prints_the_judge_errors_when_there_are_any(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_evals import cli

    past = replace(_day("d17"), judge_errors=3, cited_answers=100)
    cli._print_monitor(check_day(past, REFERENCE, judge_enabled=False), "day 17")
    shown = " ".join(capsys.readouterr().out.split())
    assert "untrusted today; 3 judge error(s) in 100 cited answers (untrusted past 2%)" in shown
    cli._print_monitor(check_day(_day("d18"), REFERENCE, judge_enabled=False), "day 18")
    assert "judge error" not in capsys.readouterr().out  # none: the line reads as before
