"""scripts/judge_compare_v6.py, stage A of judge v6 (docs/plans/judge-v6.md), offline: the results
store is a synthetic one in memory, the model is a scripted one, and every file the script writes
goes to a temporary folder. The replay check runs against the real records when they are present."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agent_evals import spend
from agent_evals.config import JudgeConfig, load_config
from agent_evals.graders.service_names import ServiceNames
from agent_evals.harness.llm import LLMResponse
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import Case
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


v5 = _script("judge_compare_v5")
jc = _script("judge_compare_v6")

QUOTE = "Bucket names must be between 3 and 63 characters long."
SPAN = "between 3 and 63 characters long"
OUTPUT = {"answer": QUOTE, "citations": [{"url": BUCKETS_URL, "quote": QUOTE}], "abstained": False}
V5_NAMED = sorted(c for c in v5.NAMED_CASES if c.startswith("docs-"))  # 19 cases
ADDED = sorted(jc.ADDED_CASES)  # the five cases the v6 design was written from
PRICING = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
CALL_USD = (100 * 2.0 + 20 * 10.0) / 1_000_000
NAMES = ServiceNames.from_data({"publisher": ["AWS", "Amazon"], "segments": {}, "pages": {}})


@dataclass(frozen=True)
class Row:
    run: str
    case_id: str
    supported: bool | None
    v2: bool | None
    labeler: str = "aleena"
    side: str = "judge-dev"
    attempt: int = 1


def design_rows() -> list[Row]:
    """The tuning set as both designs count it. v5's 19 named answers: 4 labeled supported, of
    which v2 passes the first (v5's one kept answer), and v2 makes 6 false passes and 3 false
    fails on them. The five added cases on 13 answers: 6 of them kept (labeled supported and
    passed by v2), the known miss docs-mix-058 among them, and v2 right on every one. 93 others,
    24 labeled supported, on which v2 makes no error."""
    named = [
        Row(v5.RUNS[0], case_id, n < 4, n == 0 or 4 <= n < 10) for n, case_id in enumerate(V5_NAMED)
    ]
    added = [
        Row(v5.RUNS[1], "docs-ai-003-p1", True, True),
        Row(v5.RUNS[2], "docs-ai-003-p1", True, True),
        Row(v5.RUNS[0], "docs-ai-003-p1", False, False),
        Row(v5.RUNS[1], "docs-ai-003-p3", True, True),
        Row(v5.RUNS[2], "docs-ai-003-p3", True, True),
        Row(v5.RUNS[0], "docs-ai-003-p3", False, False),
        Row(v5.RUNS[2], "docs-chg-010", True, True),
        Row(v5.RUNS[1], "docs-chg-010", False, False),
        Row(v5.RUNS[0], "docs-chg-010", False, False),
        Row(v5.RUNS[2], "docs-mix-042", False, False),
        Row(v5.RUNS[0], "docs-mix-042", False, False),
        Row(v5.RUNS[2], "docs-mix-058", True, True),
        Row(v5.RUNS[0], "docs-mix-058", False, False),
    ]
    others = [Row(v5.RUNS[n % 3], f"docs-x-{n:03d}", n < 24, n < 24) for n in range(93)]
    return named + added + others


class FakeStore:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.closed = False

    def run_info(self, run_id: str) -> dict[str, Any]:
        note = {"version": ONLINE_JUDGE_VERSION, "salt": "judge"}
        return {"run_id": run_id, "online": False, "manifest": {"judge": note}}

    def labels(self, run_id: str, labeler: str | None = None) -> list[dict[str, Any]]:
        found = [
            {
                "case_id": r.case_id,
                "attempt": r.attempt,
                "labeler": r.labeler,
                "supported": r.supported,
                "responsive": True,
                "note": "",
                "label_split": r.side,
                "core_supported": r.supported,
            }
            for r in self.rows
            if r.run == run_id and labeler in (None, r.labeler)
        ]
        return sorted(found, key=lambda x: (x["case_id"], x["attempt"]))

    def grade_reasons(
        self, run_id: str, grader: str
    ) -> dict[tuple[str, int], dict[str, tuple[float, str]]]:
        assert grader == "judge"
        return {
            (r.case_id, r.attempt): {
                "judge_core_supported": (float(r.v2), "v2"),
                "judge_supported": (float(r.v2), "v2"),
            }
            for r in self.rows
            if r.run == run_id and r.v2 is not None
        }

    def all_attempts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "case_id": r.case_id,
                "attempt": r.attempt,
                # Each run's answer is its own text, as real answers are, so no two of the 125
                # share a request and a cached reply.
                "output": {**OUTPUT, "answer": f"{QUOTE} (from {run_id})"},
                "error": None,
            }
            for r in self.rows
            if r.run == run_id
        ]

    def close(self) -> None:
        self.closed = True


def dev_cases(rows: list[Row]) -> dict[str, Case]:
    return {r.case_id: make_case(id=r.case_id, suite="capability", split="dev") for r in rows}


def tuning() -> list[Any]:
    rows = design_rows()
    answers = list(v5.load_tuning_set(FakeStore(rows), dev_cases(rows)))
    jc.check_tuning_set(answers)
    return answers


def claim_config(**overrides: Any) -> JudgeConfig:
    base = {
        "method": "claims",
        "grader_name": "judge-v6",
        "rubric_version": "v5",
        "responsive": False,
        "support_threshold": 69,
        "skeptic_threshold": None,
        "gap_types": False,
        "model": {"name": "claude-sonnet-5-5", "max_tokens": 8192, "pricing": PRICING},
    }
    return JudgeConfig.model_validate(base | overrides)


Key = tuple[str, str]  # (salt's first part, answer key: run id / case id)
RUN_IN_ANSWER = re.compile(r"\(from (docs-[0-9a-f-]+)\)")


class Judges(ScriptedLLM):
    """A scripted claim judge that replies by salt, so answers can be judged in any order. One
    core claim per answer, scored 90 when the answer is labeled supported and 30 when not, with
    the gap type "none" when the request's tool asks for one; the skeptic's search finds nothing.
    `scores` and `gaps` say otherwise for a salt and a case; `broken` makes that many claim
    tables fail (2: a judge error)."""

    def __init__(
        self,
        answers: list[Any],
        *,
        scores: dict[Key, int] | None = None,
        gaps: dict[Key, tuple[str, str]] | None = None,
        broken: dict[Key, int] | None = None,
    ) -> None:
        super().__init__([])
        self.answers = {a.key: a for a in answers}
        self.scores = scores or {}
        self.gaps = gaps or {}
        self.broken = broken or {}
        self.lock = threading.Lock()

    def reply(self, request: dict[str, Any], salt: str) -> list[dict[str, Any]]:
        name, call, case_id = salt.split("#")
        content = str(request["messages"][0]["content"])
        run = RUN_IN_ANSWER.search(content)
        assert run is not None, content[:80]
        answer = self.answers[f"{run.group(1)}/{case_id}"]
        key = (name, answer.key)
        fails = self.broken.get(key, 0)
        if (call == "claims" and fails >= 1) or (call == "claims-retry" and fails >= 2):
            return [text("I cannot make the table.")]
        if call.startswith("claims"):
            claim: dict[str, Any] = {
                "answer_span": SPAN,
                "role": "core",
                "support": [{"quote": 1, "quote_span": SPAN}],
                "score": self.scores.get(key, 90 if answer.supported else 30),
                "why": "q",
            }
            schema = request["tools"][0]["input_schema"]["properties"]["claims"]["items"]
            if "gap" in schema["properties"]:
                gap, term = self.gaps.get(key, ("none", ""))
                claim |= {"gap": gap, "term": term}
            return [tool_use("submit_claims", {"claims": [claim], "not_claims": []})]
        assert call.startswith("skeptic"), call
        search = {"found": False, "answer_span": "", "role": "core", "why": "", "confidence": 0}
        return [tool_use("submit_skeptic", {"flagged": [], "search": search})]

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        with self.lock:
            self.script = [self.reply(request, salt)]
            return super().create(request, salt=salt)

    def salts_of(self, kind: str) -> list[str]:
        return [s for s in self.salts if s.split("#")[1] == kind]


class Cached:
    """The call cache in front of a model: a request and salt asked again is replayed, free."""

    def __init__(self, llm: Judges) -> None:
        self.llm = llm
        self.seen: dict[str, LLMResponse] = {}

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        key = json.dumps([request, salt], sort_keys=True)
        if key in self.seen:
            return self.seen[key].model_copy(update={"cache_hit": True})
        self.seen[key] = self.llm.create(request, salt=salt)
        return self.seen[key]


@pytest.fixture
def records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """v2's replayed passes and v5's choice record, as the fake tuning set would give them (its
    extra passes repeat the stored verdicts), and the round's folder, all in tmp_path."""
    answers = tuning()
    extra = {
        "judge_version": ONLINE_JUDGE_VERSION,
        "salts": list(jc.V2_REPLAYED_SALTS),
        "verdicts": [
            {"key": a.key, "salt": salt, "judge_supported": a.v2_passed}
            for salt in jc.V2_REPLAYED_SALTS
            for a in answers
        ],
    }
    extra_path = tmp_path / "v2-extra-passes.json"
    extra_path.write_text(json.dumps(extra), encoding="utf-8")
    by_key = {a.key: a for a in answers}
    passes = [{a.key: a.v2_passed for a in answers}] * 3
    choice = {"v2": {}}
    for side, wanted in (("others", lambda a: not a.named), ("named", lambda a: a.named)):
        c = jc.count([a.key for a in answers if wanted(a)], passes, by_key)
        choice["v2"][side] = {
            "totals": {"false_passes": c.false_passes, "false_fails": c.false_fails}
        }
    choice_path = tmp_path / "stage-a-choice-v5.json"
    choice_path.write_text(json.dumps(choice), encoding="utf-8")
    monkeypatch.setattr(jc, "V2_EXTRA_PASSES", extra_path)
    monkeypatch.setattr(jc, "V5_CHOICE", choice_path)
    out = tmp_path / "judge-v6"
    monkeypatch.setattr(spend, "V6_PATH", out / "spend.json")
    return out


def replay_passed(out: Path, names: ServiceNames = NAMES) -> None:
    out.mkdir(parents=True, exist_ok=True)
    record = {
        "grading_code_version": jc.GRADING_CODE_VERSION,
        "rules_version": jc.RULES_VERSION,
        "service_names_sha256": names.sha256,
        "passed": True,
    }
    (out / jc.REPLAY_FILE).write_text(json.dumps(record), encoding="utf-8")


def run_stage_a(
    label: str,
    llm: Any,
    out: Path,
    *,
    gap_types: bool = False,
    answers: Sequence[Any] | None = None,
) -> Any:
    return jc.stage_a(
        label,
        gap_types,
        list(answers or tuning()),
        llm,
        claim_config(),
        NAMES,
        judge_round=spend.round_for("judge-v6"),
        out_dir=out,
    )


# ---- the tuning set and the counts -------------------------------------------------------------


def test_the_tuning_set_splits_into_93_capped_32_named_and_7_kept_with_the_known_miss() -> None:
    answers = tuning()
    assert len(answers) == 125 and sum(jc.named(a) for a in answers) == 32
    assert sum(a.named for a in answers) == 19  # v5's split still reads
    assert len(jc.kept(answers)) == 7 and len(jc.kept_rule(answers)) == 6
    assert [
        a.case.id for a in jc.kept(answers) if a.case.id not in jc.kept_rule(answers)
    ] == [] or ([a.case.id for a in jc.kept(answers)].count("docs-mix-058") == 1)
    v2 = jc.read(answers, [jc.stored_v2(answers)])
    assert (v2.capped.errors, v2.named.false_passes, v2.named.false_fails) == (0, 6, 3)
    assert v2.total_errors == Fraction(9) and v2.keeps(answers)


def test_the_tuning_set_refuses_another_shape() -> None:
    rows = design_rows()
    rows.append(Row(v5.RUNS[1], "docs-mix-058", True, True))  # one more kept answer
    with pytest.raises(v5.Refused):
        v5.load_tuning_set(FakeStore(rows), dev_cases(rows))  # v5's count breaks first (126)
    answers = tuning()
    with pytest.raises(jc.Refused, match="the design counts 125, 32 and 7"):
        jc.check_tuning_set(answers[1:])
    without_miss = [a for a in answers if a.case.id != jc.KNOWN_MISS]
    extra = [a for a in answers if a.case.id == "docs-ai-003-p1"][:2]  # the shape, no known miss
    with pytest.raises(jc.Refused, match="known miss"):
        jc.check_tuning_set(without_miss + extra)


def test_v2s_three_passes_are_read_from_the_committed_replay_and_checked(records: Path) -> None:
    answers = tuning()
    passes = jc.v2_passes(answers)
    assert [salt for salt, _ in passes] == ["judge", "judge-s2", "judge-s3"]
    reading = jc.read(answers, [p for _, p in passes])
    assert reading.total_errors == Fraction(9) and reading.passes == 3
    broken = json.loads(jc.V2_EXTRA_PASSES.read_text(encoding="utf-8"))
    broken["verdicts"][0]["judge_supported"] = not broken["verdicts"][0]["judge_supported"]
    jc.V2_EXTRA_PASSES.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(jc.Refused, match="not the choice record's"):
        jc.v2_passes(answers)


def test_the_gates_read_the_total_the_caps_the_kept_rule_and_the_judge_error_cap() -> None:
    answers = tuning()
    v2 = jc.read(answers, [jc.stored_v2(answers)] * 3)
    truth = {a.key: a.supported for a in answers}
    perfect = jc.read(answers, [truth] * 3)
    assert jc.gates(perfect, v2, answers, [0, 0, 0]).eligible and jc.build_check(perfect, v2)
    # Two errors per pass on the 93 and seven on the 32: at the edge of both split gates, and
    # worse than v2 overall (9 against 9 is not fewer): the total gate refuses it.
    capped = [a.key for a in answers if not jc.named(a) and not a.supported][:2]
    named = [a.key for a in answers if jc.named(a) and not a.supported][:7]
    edge = {k: (True if k in capped or k in named else v) for k, v in truth.items()}
    reading = jc.read(answers, [edge] * 3)
    g = jc.gates(reading, v2, answers, [0, 0, 0])
    assert g.caps and g.keeps and g.judge_error_cap and not g.total_gate and not g.eligible
    # The build check alone would admit it (7 against 9 on the 32): the review's point.
    assert reading.total_errors == Fraction(9) and jc.build_check(reading, v2)
    # A kept answer missed on two of three passes, or three judge errors in a pass, ends it.
    miss = next(a.key for a in jc.kept_rule(answers))
    kept_missed = jc.read(answers, [truth | {miss: False}] * 2 + [truth])
    assert not jc.gates(kept_missed, v2, answers, [0, 0, 0]).keeps
    known = next(a.key for a in jc.kept(answers) if a.case.id == jc.KNOWN_MISS)
    assert jc.gates(jc.read(answers, [truth | {known: False}] * 3), v2, answers, [0, 0, 0]).keeps
    assert not jc.gates(perfect, v2, answers, [0, 3, 0]).judge_error_cap
    assert jc.gates(perfect, v2, answers, [2, 2, 2]).judge_error_cap


def test_the_candidate_order_ranks_the_total_then_the_93_then_t_then_s() -> None:
    answers = tuning()
    truth = {a.key: a.supported for a in answers}
    unsupported_named = [a.key for a in answers if jc.named(a) and not a.supported]
    unsupported_capped = [a.key for a in answers if not jc.named(a) and not a.supported]

    def reading(named_errors: int, capped_errors: int) -> Any:
        wrong = set(unsupported_named[:named_errors]) | set(unsupported_capped[:capped_errors])
        return jc.read(answers, [{k: (True if k in wrong else v) for k, v in truth.items()}])

    keys = [
        (jc.step4_key(reading(2, 0), 69, None, 0), "a"),
        (jc.step4_key(reading(1, 1), 74, 59, 0), "b"),  # the same total as a, more on the 93
        (jc.step4_key(reading(1, 0), 74, None, 1), "c"),
        (jc.step4_key(reading(1, 0), 69, 49, 1), "d"),  # lower t beats s off
        (jc.step4_key(reading(1, 0), 69, None, 1), "e"),  # s off beats 49
        (jc.step4_key(reading(1, 0), 69, None, 0), "f"),  # the version judged first
    ]
    assert [name for _, name in sorted(keys)] == ["f", "e", "d", "c", "a", "b"]


# ---- the replay check gates the first paid pass -----------------------------------------------


def test_stage_a_waits_for_a_passed_replay_check_of_the_code_in_place(records: Path) -> None:
    llm = Judges(tuning())
    with pytest.raises(jc.Refused, match="the replay check comes first"):
        run_stage_a("draft", llm, records)
    records.mkdir(parents=True, exist_ok=True)
    (records / jc.REPLAY_FILE).write_text(json.dumps({"passed": False}), encoding="utf-8")
    with pytest.raises(jc.Refused, match="the replay check comes first"):
        run_stage_a("draft", llm, records)
    other = ServiceNames.from_data({"publisher": ["AWS"], "segments": {}, "pages": {}})
    replay_passed(records, other)  # another service-name file: another code version
    with pytest.raises(jc.Refused, match="another code version's"):
        run_stage_a("draft", llm, records)
    assert llm.requests == []


@pytest.mark.skipif(
    not (ROOT / "results" / "evals.duckdb").is_file()
    or not (ROOT / "results" / "judge-v5" / "stage-a-rev1.judge-s3.json").is_file(),
    reason="the round's records are not here",
)
def test_the_replay_check_reproduces_the_designs_figures_on_the_real_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No call is made: the code in place reads judge v5's revision 1 passes with the cited
    services' names filled in from the stored citations, and every pre-registered figure holds."""
    monkeypatch.chdir(ROOT)
    config = load_config(jc.CLAIM_CONFIG)
    cases = {case.id: case for case in load_suite(v5.DEV_SUITE)}
    store = ResultsStore(config.paths.db, read_only=True)
    try:
        answers = v5.load_tuning_set(store, cases)
    finally:
        store.close()
    jc.check_tuning_set(answers)
    record = jc.replay_check(answers, ServiceNames.load(), tmp_path)
    assert record["passed"], record["problems"]
    assert record["v2_errors_on_125"] == 24 and record["moved_answer"]["cells_passed_per_salt"] == [
        9,
        9,
        9,
    ]
    at_70 = record["figures"]["cells"]["t=69 s=off"]
    assert (at_70["total_125"], at_70["capped_93"]) == (23, {"false_passes": 0, "false_fails": 0})
    assert record["figures"]["cells"]["t=69 s=49"]["total_125"] == 18
    assert json.loads((tmp_path / jc.REPLAY_FILE).read_text(encoding="utf-8"))["passed"]


# ---- stage A -----------------------------------------------------------------------------------


def test_stage_a_runs_the_pilot_once_then_three_passes_and_reads_the_grid(records: Path) -> None:
    answers = tuning()
    replay_passed(records)
    llm = Cached(Judges(answers))
    result = run_stage_a("draft", llm, records, answers=answers)
    assert result.pilot is not None and result.pilot["stop"] is False
    assert len(result.pilot["answers"]) == 20 and result.pilot["salts"] == [
        "judge-v6-draft-1",
        "judge-v6-draft-2",
        "judge-v6-draft-3",
    ]
    judge = llm.llm
    assert len(judge.salts_of("claims")) == 3 * 125  # the pilot's 60 are read from the cache
    assert {s.split("#")[0] for s in judge.salts} == set(result.pilot["salts"])
    assert [p.name for p in result.paths] == [f"stage-a-draft.pass{n}.json" for n in (1, 2, 3)]
    assert all(p.is_file() for p in result.paths)
    ledger = spend.round_for("judge-v6").ledger()
    first, second = ledger.entries()
    assert (first.step, second.step) == ("pilot", "stage-a draft")
    assert first.uncached_calls == first.calls  # the pilot's calls are all fresh
    assert second.calls - second.uncached_calls == first.calls  # and replayed in the passes
    assert sum(e.usd for e in ledger.entries()) == pytest.approx(len(judge.salts) * CALL_USD)
    # The scripted judge agrees with every label, so every cell is eligible and beats v2.
    assert all(c.gates.eligible and c.build_check for c in result.cells)
    assert result.cells[0].reading.total_errors == 0 and result.v2.total_errors == Fraction(9)
    assert result.order[0].spec == "draft:64:off" and len(result.order) == 9
    assert result.counts.judgings == 375 and result.counts.gaps_none == 0  # v5's prompts: no gaps
    # Read back, nothing is judged again.
    again = run_stage_a("draft", Judges(answers), records, answers=answers)
    assert again.spent == [] and again.pilot is None and again.versions_counted == 1
    rendered = jc.render_stage_a(result, ledger)
    assert "pilot (step 1): 20 answers" in rendered and "candidate order (step 4)" in rendered
    assert "choose draft:64:off draft:69:off" in rendered  # the second candidate differs in t


def test_a_pilot_that_measures_too_much_stops_the_work(
    records: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_passed(records)
    # Every answer costs at least one call, so at this estimate the pilot measures over 1.3x.
    monkeypatch.setattr(spend, "V6_ESTIMATE_PER_ANSWER_USD", CALL_USD / 2)
    with pytest.raises(spend.PilotRefusal, match="the pilot recorded a stop"):
        run_stage_a("draft", Cached(Judges(tuning())), records)
    record = json.loads((records / jc.PILOT_FILE).read_text(encoding="utf-8"))
    assert record["stop"] is True
    [entry] = spend.round_for("judge-v6").ledger().entries()
    assert entry.step == "pilot" and not (records / "stage-a-draft.pass1.json").exists()


def test_stage_a_judges_each_version_once_under_one_label_and_at_most_four(records: Path) -> None:
    answers = tuning()
    replay_passed(records)
    run_stage_a("draft", Cached(Judges(answers)), records, answers=answers)
    with pytest.raises(jc.Refused, match="judged already, as 'draft'"):
        run_stage_a("draft2", Judges(answers), records, answers=answers)
    with pytest.raises(jc.Refused, match="another version's pass"):
        run_stage_a("draft", Judges(answers), records, answers=answers, gap_types=True)
    rev1 = run_stage_a("rev1", Cached(Judges(answers)), records, answers=answers, gap_types=True)
    assert rev1.gap_types and rev1.counts.gaps_none == 375 and rev1.versions_counted == 2
    assert rev1.pilot is None  # the pilot ran on the first version
    for label in ("rev2", "rev3"):
        with pytest.raises(jc.Refused, match="judged already"):  # same fingerprint as rev1
            run_stage_a(label, Judges(answers), records, answers=answers, gap_types=True)


def test_stage_a_reads_gap_types_the_rule_fails_and_the_kept_rule(records: Path) -> None:
    answers = tuning()
    replay_passed(records)
    kept = next(a for a in jc.kept_rule(answers))
    known = next(a for a in jc.kept(answers) if a.case.id == jc.KNOWN_MISS)
    gaps = {(f"judge-v6-rev1-{n}", kept.key): ("term", "Harness") for n in (1, 2)} | {
        (f"judge-v6-rev1-{n}", known.key): ("cell", "") for n in (1, 2, 3)
    }
    llm = Cached(Judges(answers, gaps=gaps))
    result = run_stage_a("rev1", llm, records, answers=answers, gap_types=True)
    assert (result.counts.gaps_term, result.counts.gaps_cell, result.counts.rule_fails) == (2, 3, 5)
    assert result.counts.terms_unquoted == 2
    cell = result.cells[0]
    assert cell.reading.kept_passes[kept.key] == 1 and cell.reading.kept_passes[known.key] == 0
    assert not cell.gates.keeps and not cell.gates.eligible  # the kept answer fails 2 of 3
    assert result.order == []
    rendered = jc.render_stage_a(result, spend.round_for("judge-v6").ledger())
    assert "known miss" in rendered and "no cell of any version is eligible yet" in rendered


def test_a_judge_error_counts_on_the_named_cases_and_the_125_and_the_cap_reads_every_pass(
    records: Path,
) -> None:
    answers = tuning()
    replay_passed(records)
    named = [a for a in answers if jc.named(a) and not a.supported][:3]
    broken = {("judge-v6-draft-2", a.key): 2 for a in named}
    result = run_stage_a("draft", Cached(Judges(answers, broken=broken)), records, answers=answers)
    cell = result.cells[0]
    assert cell.judge_errors_by_pass == [0, 3, 0] and not cell.gates.judge_error_cap
    assert cell.reading.named.judge_errors == 3 and cell.reading.total_errors == Fraction(3, 3)
    assert cell.reading.capped.errors == 0 and result.counts.judge_errors == 3


# ---- the choice --------------------------------------------------------------------------------


def choose(specs: list[str], llm: Any, out: Path, answers: Sequence[Any]) -> Any:
    return jc.choose(
        specs,
        list(answers),
        llm,
        claim_config(),
        NAMES,
        judge_round=spend.round_for("judge-v6"),
        out_dir=out,
    )


def test_choose_judges_fresh_samples_only_and_freezes_the_first_eligible_candidate(
    records: Path,
) -> None:
    answers = tuning()
    replay_passed(records)
    judge = Judges(answers)
    cached = Cached(judge)
    run_stage_a("draft", cached, records, answers=answers)
    with pytest.raises(jc.Refused, match="the candidates are step 4's first cell"):
        choose(["draft:69:off"], cached, records, answers)
    # Under the choice salts the cache has nothing, so every call is fresh; a replayed call is
    # refused.
    result = choose(["draft:64:off", "draft:69:off"], cached, records, answers)
    assert result.chosen is not None and result.stop is None
    assert (result.chosen.label, result.chosen.t, result.chosen.s) == ("draft", 64, None)
    assert {s.split("#")[0] for s in judge.salts_of("claims")[-375:]} == {
        "judge-v6-choice-1",
        "judge-v6-choice-2",
        "judge-v6-choice-3",
    }
    record = json.loads(result.path.read_text(encoding="utf-8"))
    assert record["frozen"] == {
        "label": "draft",
        "t": 64,
        "s": None,
        "gap_types": False,
        "version": record["candidates"][0]["version"],
    }
    assert (
        record["candidates"][0]["build_check"] is True
        and record["v2"]["total_errors_per_pass"] == 9.0
    )
    entries = spend.round_for("judge-v6").ledger().entries()
    assert entries[-1].step == "choose draft:64:off draft:69:off" and entries[
        -1
    ].uncached_calls == entries[-1].calls == 3 * (125 + sum(a.supported for a in answers))
    with pytest.raises(jc.Refused, match="the choice is made once"):
        choose(["draft:64:off", "draft:69:off"], cached, records, answers)
    with pytest.raises(jc.Refused, match="no version is judged after it"):
        run_stage_a("rev1", Judges(answers), records, answers=answers, gap_types=True)
    assert "FROZEN: draft, t = 64, s = off" in jc.render_choice(
        result, spend.round_for("judge-v6").ledger()
    )


def test_choose_refuses_a_call_the_cache_serves_under_a_choice_salt(records: Path) -> None:
    answers = tuning()
    replay_passed(records)
    cached = Cached(Judges(answers))
    run_stage_a("draft", cached, records, answers=answers)
    # One answer judged under the first choice salt beforehand: its reply is in the cache, so
    # the choice's call for it is a replay, and the step refuses before anything is frozen.
    jc.judge_pass(cached, claim_config(), NAMES, [answers[0]], "judge-v6-choice-1")
    with pytest.raises(jc.Refused, match="served by the call cache"):
        choose(["draft:64:off", "draft:69:off"], cached, records, answers)
    assert not (records / jc.CHOICE_FILE).exists()


def test_choose_stops_the_round_when_no_candidate_is_eligible_on_its_fresh_means(
    records: Path,
) -> None:
    answers = tuning()
    replay_passed(records)
    judge = Judges(answers)
    run_stage_a("draft", Cached(judge), records, answers=answers)
    # Under the choice salts, the judge passes every unsupported named answer and a dozen
    # unsupported others: worse than v2 overall at every cell.
    wrong = [a for a in answers if not a.supported][:40]
    judge.scores |= {(f"judge-v6-choice-{n}", a.key): 95 for n in (1, 2, 3) for a in wrong}
    result = choose(["draft:64:off", "draft:69:off"], Cached(judge), records, answers)
    assert result.chosen is None and result.stop is not None
    assert "no candidate is eligible on its fresh means" in result.stop
    assert not any(c.gates.total_gate for c in result.ordered)
    record = json.loads(result.path.read_text(encoding="utf-8"))
    assert record["frozen"] is None and record["chosen"] is None
    assert "STOP:" in jc.render_choice(result, spend.round_for("judge-v6").ledger())


def test_choose_stops_before_any_call_when_no_cell_is_eligible(records: Path) -> None:
    answers = tuning()
    replay_passed(records)
    wrong = [a for a in answers if not a.supported][:40]
    judge = Judges(
        answers, scores={(f"judge-v6-draft-{n}", a.key): 95 for n in (1, 2, 3) for a in wrong}
    )
    result = run_stage_a("draft", Cached(judge), records, answers=answers)
    assert result.order == []
    fresh = Judges(answers)
    with pytest.raises(jc.Refused, match="no cell of any version is eligible"):
        choose(["draft:64:off"], fresh, records, answers)
    assert fresh.requests == []
