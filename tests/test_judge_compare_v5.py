"""scripts/judge_compare_v5.py, stage A of the claim judge (docs/plans/judge-v5.md), offline: the
results store is a synthetic one in memory, the model is ScriptedLLM, and every file the script
writes goes to a temporary folder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_evals.config import JudgeConfig
from agent_evals.graders.claim_judge import (
    Assessment,
    CheckedClaim,
    CheckedSupport,
    ClaimVerdict,
    Search,
    SkepticReply,
)
from agent_evals.harness.llm import LLMResponse
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.spend import PilotRefusal, SpendCapError, SpendLedger
from agent_evals.suites import write_suite
from agent_evals.types import Case
from conftest import BUCKETS_URL, ScriptedLLM, make_case, text, tool_use

ROOT = Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    """The script as a module, registered under its name: dataclasses and pydantic look it up."""
    path = ROOT / "scripts" / "judge_compare_v5.py"
    spec = importlib.util.spec_from_file_location("judge_compare_v5", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


jc = _script()

QUOTE = "Bucket names must be between 3 and 63 characters long."
SPAN = "between 3 and 63 characters long"
OUTPUT = {"answer": QUOTE, "citations": [{"url": BUCKETS_URL, "quote": QUOTE}], "abstained": False}
NAMED = sorted(case_id for case_id in jc.NAMED_CASES if case_id.startswith("docs-"))  # 19
PRICING = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
CALL_USD = (100 * 2.0 + 20 * 10.0) / 1_000_000  # each ScriptedLLM reply: 100 tokens in, 20 out
CELL_NAMES = {f"t={t} s={'off' if s is None else s}" for t in (50, 70, 85) for s in (None, 70, 90)}


@dataclass(frozen=True)
class Row:
    """One stored answer of a dev run, with its label and v2's stored verdict."""

    run: str
    case_id: str
    supported: bool | None
    v2: bool | None  # None: no stored verdict
    labeler: str = "aleena"
    side: str = "judge-dev"
    attempt: int = 1


def design_rows() -> list[Row]:
    """The tuning set as the design counts it. 19 named answers: 4 labeled supported, of which v2
    passes the first (the one a new judge must keep), and v2 makes 6 false passes and 3 false
    fails on them. 106 others, 30 labeled supported, on which v2 makes no error."""
    named = [
        Row(jc.RUNS[0], case_id, n < 4, n == 0 or 4 <= n < 10) for n, case_id in enumerate(NAMED)
    ]
    others = [Row(jc.RUNS[n % 3], f"docs-x-{n:03d}", n < 30, n < 30) for n in range(106)]
    return named + others


# What the tuning set leaves out: another labeler's label, the other label side, no support
# label (an uncited answer), a later attempt, and no v2 verdict to join.
LEFT_OUT = [
    Row(jc.RUNS[0], "docs-x-000", False, True, labeler="someone-else"),
    Row(jc.RUNS[1], "docs-y-001", True, True, side="judge-test"),
    Row(jc.RUNS[1], "docs-y-002", None, None),
    Row(jc.RUNS[2], "docs-y-003", True, True, attempt=2),
    Row(jc.RUNS[2], "docs-y-004", False, None),
]


class FakeStore:
    """The results store's reads over synthetic rows, in memory. It has no way to write."""

    def __init__(
        self, rows: list[Row], *, note: dict[str, Any] | None = None, online: bool = False
    ) -> None:
        self.rows = rows
        self.note = note if note is not None else {"version": ONLINE_JUDGE_VERSION, "salt": "judge"}
        self.online = online
        self.closed = False

    def run_info(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "online": self.online, "manifest": {"judge": self.note}}

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
        assert grader == "judge"  # only v2's stored verdicts are read
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
            {"case_id": r.case_id, "attempt": r.attempt, "output": OUTPUT, "error": None}
            for r in self.rows
            if r.run == run_id
        ]

    def close(self) -> None:
        self.closed = True


def dev_cases(rows: list[Row]) -> dict[str, Case]:
    return {r.case_id: make_case(id=r.case_id, suite="capability", split="dev") for r in rows}


def tuning() -> list[Any]:
    rows = design_rows() + LEFT_OUT
    return list(jc.load_tuning_set(FakeStore(rows), dev_cases(rows)))


def claims_reply(
    score: int, *, dropped: bool = False, misquoted: bool = False
) -> list[dict[str, Any]]:
    """One core claim scored `score`, quoted from quote 1. `dropped` adds a second entry, for a
    quote the answer does not cite, which the span check drops; `misquoted` cites only that one,
    so the claim keeps no support entry."""
    quoted, unknown = {"quote": 1, "quote_span": SPAN}, {"quote": 9, "quote_span": SPAN}
    support = [unknown] if misquoted else [quoted, unknown] if dropped else [quoted]
    claim = {"answer_span": SPAN, "role": "core", "support": support, "score": score, "why": "q"}
    return [tool_use("submit_claims", {"claims": [claim], "not_claims": []})]


def skeptic_reply(confidence: int | None, items: Sequence[int] = ()) -> list[dict[str, Any]]:
    """The skeptic's search names a core claim at this confidence, or finds nothing; each flagged
    item it is asked about scores 90, as quoted."""
    found = confidence is not None
    search = {
        "found": found,
        "answer_span": "Bucket names must be" if found else "",
        "role": "core",
        "why": "no quote says so",
        "confidence": confidence or 0,
    }
    flagged = [{"item": n, "score": 90, "not_a_claim": False, "why": "quoted"} for n in items]
    return [tool_use("submit_skeptic", {"flagged": flagged, "search": search})]


def flagged_items(request: dict[str, Any]) -> list[int]:
    """The numbers of the flagged items a skeptic request lists."""
    listed = str(request["messages"][0]["content"]).rsplit("Flagged items:\n", 1)[1]
    return [int(n) for n in re.findall(r"(?m)^(\d+)\. ", listed)]


Key = tuple[str, str]  # (salt, case id)


class Judges(ScriptedLLM):
    """ScriptedLLM that replies by salt, so answers can be judged in any order. The claim table
    gives each answer one claim, scored 90 when it is labeled supported and 30 when not; the
    skeptic's search finds nothing; v2 repeats its stored verdict. `scores`, `searches` and `v2`
    say otherwise for a salt and a case (v2 None: a reply with no verdict); `broken` makes that
    many claim tables fail (1: retried, 2: a judge error); `dropped` adds an entry the span
    check drops, and `misquoted` leaves the claim only that one; a call about a case in `down`
    fails as the API would."""

    def __init__(
        self,
        answers: list[Any],
        *,
        scores: dict[Key, int] | None = None,
        searches: dict[Key, int] | None = None,
        v2: dict[Key, bool | None] | None = None,
        broken: dict[Key, int] | None = None,
        dropped: set[Key] | None = None,
        misquoted: set[Key] | None = None,
        down: set[str] | None = None,
    ) -> None:
        super().__init__([])
        self.answers = {a.case.id: a for a in answers}
        self.scores = scores or {}
        self.searches = searches or {}
        self.v2 = v2 or {}
        self.broken = broken or {}
        self.dropped = dropped or set()
        self.misquoted = misquoted or set()
        self.down = down or set()
        self.lock = threading.Lock()

    def reply(self, request: dict[str, Any], salt: str) -> list[dict[str, Any]]:
        name, call, case_id = salt.split("#")
        if case_id in self.down:
            raise RuntimeError("the API is down")
        answer, key = self.answers[case_id], (name, case_id)
        fails = self.broken.get(key, 0)
        if (call == "claims" and fails >= 1) or (call == "claims-retry" and fails >= 2):
            return [text("I cannot make the table.")]
        if call.startswith("claims"):
            score = self.scores.get(key, 90 if answer.supported else 30)
            return claims_reply(score, dropped=key in self.dropped, misquoted=key in self.misquoted)
        if call.startswith("skeptic"):
            return skeptic_reply(self.searches.get(key), flagged_items(request))
        assert call == "supported", call  # v2's support call; never its responsive one
        verdict = self.v2.get(key, answer.v2_passed)
        if verdict is None:
            return [text("No verdict.")]
        word = "pass" if verdict else "fail"
        given = {"core_supported": word, "fully_supported": word, "reason": "v2"}
        return [tool_use("submit_support_verdict", given)]

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        with self.lock:  # the script may judge answers in parallel
            self.script = [self.reply(request, salt)]
            return super().create(request, salt=salt)

    def calls(self, kind: str, since: int = 0) -> list[str]:
        """The salts of the calls of one kind (claims, skeptic, supported) made since then."""
        return [s for s in self.salts[since:] if s.split("#")[1] == kind]


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


def claim_config(**overrides: Any) -> JudgeConfig:
    """The claim judge as configs/judge-v5.yaml sets it, priced. Another rubric or tool schema
    stands for another version of its code."""
    base = {
        "method": "claims",
        "grader_name": "judge-v5",
        "rubric_version": "v5",
        "responsive": False,
        "model": {"name": "claude-sonnet-5-5", "max_tokens": 8192, "pricing": PRICING},
    }
    return JudgeConfig.model_validate(base | overrides)


def v2_config() -> JudgeConfig:
    """v2 as configs/default.yaml sets it: responsive is left on there, and the script asks
    for support only."""
    model = {"name": "claude-sonnet-5-5", "max_tokens": 2048, "pricing": PRICING}
    return JudgeConfig.model_validate({"model": model})


def pilot_done(out: Path, cost: float = 0.02) -> None:
    """A pilot's record: the cost per answer later steps are estimated at, and the version it
    judged, which is the first draft: claim_config's."""
    out.mkdir(parents=True, exist_ok=True)
    judged = jc.versions(ScriptedLLM([]), claim_config())
    record = {"cost_per_answer_usd": cost, "versions": judged}
    (out / "pilot.json").write_text(json.dumps(record), encoding="utf-8")


def assessment(score: int, search: int | None = None, *, dropped: bool = False) -> Assessment:
    """One core claim scored `score`, quoted unless its one support entry was dropped, and a
    skeptic whose search names a core claim at confidence `search`."""
    problem = "not in quote 1" if dropped else None
    support = [CheckedSupport(quote=1, quote_span=SPAN, problem=problem)]
    claim = CheckedClaim(answer_span=SPAN, role="core", support=support, score=score, why="q")
    found = Search(
        found=search is not None,
        answer_span="Bucket names must be" if search is not None else "",
        role="core",
        why="no quote says so",
        confidence=search or 0,
    )
    reply = SkepticReply(flagged=[], search=found)
    return Assessment(claims=[claim], skeptic=reply, dropped_support=int(dropped))


def agreeing(answers: list[Any]) -> dict[str, Assessment]:
    """The claim judge agreeing with every label."""
    return {a.key: assessment(90 if a.supported else 30) for a in answers}


def verdicts_of(found: dict[str, Assessment]) -> dict[str, Any]:
    """Each assessment's verdicts at the nine cells, as a pass saves them when it is judged."""
    return {key: jc.read_verdicts(a) for key, a in found.items()}


def reading(
    fp: int = 0,
    ff: int = 0,
    *,
    named: int = 0,
    je: int = 0,
    passes: int = 1,
    keeps: bool = True,
) -> Any:
    """A reading with these totals over its passes: false passes and false fails outside the
    named cases, and errors and judge errors on them."""
    return jc.Reading(
        others=jc.Counts(106, passes, fp, ff, 0),
        named=jc.Counts(19, passes, named, 0, je),
        keeps=keeps,
    )


def test_the_tuning_set_is_the_designs_125_answers_in_case_id_order() -> None:
    answers = tuning()
    assert len(answers) == 125 and sum(a.named for a in answers) == 19
    order = [(a.case.id, a.run_id) for a in answers]
    assert order == sorted(order) and order[0] == (NAMED[0], jc.RUNS[0])
    assert [a.key for a in jc.to_keep(answers)] == [f"{jc.RUNS[0]}/{NAMED[0]}"]
    assert not {a.case.id for a in answers} & {r.case_id for r in LEFT_OUT[1:]}
    assert next(a for a in answers if a.case.id == "docs-x-000").supported  # the author's label
    v2 = jc.read(answers, [jc.stored_v2(answers)])
    assert (v2.others.false_passes, v2.others.false_fails) == (0, 0)
    assert (v2.named.false_passes, v2.named.false_fails, v2.keeps) == (6, 3, True)


def test_the_tuning_set_refuses_anything_but_the_designs() -> None:
    rows = design_rows()
    cases = dev_cases(rows)
    on_test = {**cases, NAMED[3]: cases[NAMED[3]].model_copy(update={"split": "test"})}
    not_in_suite = {k: v for k, v in cases.items() if k != "docs-x-005"}
    for held in (on_test, not_in_suite):
        with pytest.raises(jc.Refused, match="not a dev-split case"):
            jc.load_tuning_set(FakeStore(rows), held)
    a_second_to_keep = [replace(rows[1], v2=True), *rows[:1], *rows[2:]]
    for wrong in (rows[:-1], a_second_to_keep):
        with pytest.raises(jc.Refused, match="the design counts 125, 19 and 1"):
            jc.load_tuning_set(FakeStore(wrong), cases)
    for store in (
        FakeStore(rows, online=True),
        FakeStore(rows, note={"version": "9e7b588e83f8", "salt": "judge"}),
        FakeStore(rows, note={}),
    ):
        with pytest.raises(jc.Refused, match="must be a dev run"):
            jc.load_tuning_set(store, cases)
    resampled = FakeStore(rows, note={"version": ONLINE_JUDGE_VERSION, "salt": "judge-s2"})
    with pytest.raises(jc.Refused, match="under salt 'judge-s2'"):
        jc.load_tuning_set(resampled, cases)


def test_the_grid_counts_each_cells_errors_outside_the_named_cases_and_on_them() -> None:
    answers = tuning()
    found = agreeing(answers)
    others = [a for a in answers if not a.named]
    strict = next(a for a in others if a.supported)
    lenient, failed = [a for a in others if not a.supported][:2]
    keep = jc.to_keep(answers)[0]
    found[strict.key] = assessment(60)  # a false fail from t = 70
    found[lenient.key] = assessment(95, search=80)  # a false pass unless s = 70 catches it
    found[failed.key] = Assessment(error="the claim table failed twice")
    found[keep.key] = assessment(80)  # kept at t = 50 and 70, lost at 85
    cells = {(c.t, c.s): c for c in jc.grid("draft", 0, answers, verdicts_of(found))}

    def errors(c: Any) -> tuple[int, int, int, int]:
        others, named = c.reading.others, c.reading.named
        return others.false_passes, others.false_fails, named.false_passes, named.false_fails

    assert len(cells) == 9
    assert errors(cells[50, None]) == (1, 0, 0, 0)
    assert errors(cells[50, 70]) == (0, 0, 0, 0)
    assert errors(cells[50, 90]) == (1, 0, 0, 0)  # 80 is under s = 90
    assert errors(cells[70, None]) == (1, 1, 0, 0)
    assert errors(cells[85, 70]) == (0, 1, 0, 1)  # the answer to keep, lost
    assert all(c.reading.others.judge_errors == 1 for c in cells.values())  # never an error
    assert all(
        c.reading.others.errors == c.reading.others.errors_and_judge_errors - 1
        for c in cells.values()
    )
    assert [cells[t, None].reading.keeps for t in jc.T_GRID] == [True, True, False]
    assert all(cells[t, s].eligible for t in (50, 70) for s in jc.S_GRID)  # 2 errors, 1 FF
    assert not any(cells[85, s].eligible for s in jc.S_GRID)
    assert (cells[50, 70].reading.others.answers, cells[50, 70].reading.named.answers) == (106, 19)


def test_a_fail_that_only_a_dropped_span_explains_is_counted() -> None:
    answers = tuning()
    found = agreeing(answers)
    others = [a for a in answers if not a.named]
    flip = next(a for a in others if a.supported)
    steady = next(a for a in others if not a.supported)
    found[flip.key] = assessment(80, dropped=True)  # with its entry back it passes at 50 and 70
    found[steady.key] = assessment(30, dropped=True)  # fails either way
    saved = verdicts_of(found)
    cells = {(c.t, c.s): c for c in jc.grid("draft", 0, answers, saved)}
    assert [cells[t, None].dropped_span_flips for t in jc.T_GRID] == [1, 1, 0]
    assert cells[50, None].reading.others.false_fails == 1
    assert [saved[flip.key].cells[f"t={t} s=off"].dropped_span_flip for t in jc.T_GRID] == [
        True,
        True,
        False,  # it fails at t = 85 with its entry back too
    ]
    assert not any(c.dropped_span_flip for c in saved[steady.key].cells.values())


def test_a_cell_is_eligible_at_v2s_errors_plus_2_with_false_fails_at_v2s_plus_1() -> None:
    v2 = reading()
    assert jc.eligible(reading(1, 1), v2) and jc.eligible(reading(2, 0), v2)
    assert not jc.eligible(reading(2, 1), v2)  # 3 errors
    assert not jc.eligible(reading(0, 2), v2)  # 2 false fails
    assert not jc.eligible(reading(keeps=False), v2)  # the named answer v2 passes, lost
    assert jc.eligible(reading(named=19), v2)  # the named cases rank cells; they do not gate
    v2_three = reading(0, 1, passes=3)  # on three passes, means: exactly 1/3 a pass
    assert jc.eligible(reading(3, 4, passes=3), v2_three)  # 7/3 errors and 4/3 FF: at both caps
    assert not jc.eligible(reading(4, 4, passes=3), v2_three)  # 8/3 errors
    assert not jc.eligible(reading(2, 5, passes=3), v2_three)  # 5/3 false fails


def test_the_candidate_order_ranks_named_errors_then_the_others_then_t_then_s() -> None:
    def cell(spec: str, version: int, named: int, others: int, ok: bool = True) -> Any:
        label, t, s = jc.parse_candidate(spec)
        return jc.Cell(label, version, t, s, reading(others, named=named), ok, 0)

    cells = [
        cell("draft:50:off", 0, named=4, others=0),
        cell("draft:70:70", 0, named=2, others=1),
        cell("draft:85:off", 0, named=0, others=0, ok=False),  # the best, but not eligible
        cell("rev1:70:90", 1, named=2, others=1),
        cell("rev1:70:off", 1, named=2, others=1),
        cell("rev1:50:70", 1, named=2, others=1),
        cell("rev2:70:off", 2, named=2, others=1),
        cell("rev2:50:90", 2, named=2, others=2),
    ]
    assert [c.spec for c in jc.candidate_order(cells)] == [
        "rev1:50:70",  # the fewest errors on the named, then outside them, then the lowest t
        "rev1:70:off",  # then s off,
        "rev2:70:off",  # (the same cell of a later version, after it)
        "rev1:70:90",  # then 90,
        "draft:70:70",  # then 70
        "rev2:50:90",  # more errors outside the named cases
        "draft:50:off",  # more on them, whatever else
    ]


def candidate(r: Any, t: int = 50) -> Any:
    """A candidate cell of the draft on three passes, eligible, with no event to report."""
    nothing = jc.Events(375, 0, 0, 375, 0, 0, 0)
    return jc.Candidate("draft", t, None, 0, "abc", r, True, nothing, 0)


def test_a_judge_error_on_a_named_case_counts_as_an_error_where_the_design_says() -> None:
    # An answer the judge cannot judge is not fixed: on the named cases each judge error is an
    # error, in step 4's ranking and in step 6's build check; outside them it is in no count.
    erring, right = reading(named=7, je=2), reading(named=8)
    assert erring.named.errors_and_judge_errors == 9 and erring.named.errors == 7
    assert jc.step4_key(right, 50, None, 1) < jc.step4_key(erring, 50, None, 0)
    v2 = reading(named=27, passes=3)  # 9 errors on the named cases a pass
    same = candidate(reading(named=21, je=6, passes=3))  # 7 errors and 2 judge errors a pass
    _, chosen, stop = jc.decide([same], v2)
    assert chosen is same and stop is not None and "no fewer errors than v2" in stop
    assert jc.decide([candidate(reading(named=20, je=6, passes=3))], v2)[2] is None
    # For v2 too: 7 errors and 2 replies with no verdict a pass are 9 a pass, not 7.
    v2_unsure = jc.Reading(v2.others, jc.Counts(19, 3, 21, 0, 6), True)
    assert jc.decide([candidate(reading(named=24, passes=3))], v2_unsure)[2] is None
    outside = jc.Reading(jc.Counts(106, 1, 0, 0, 5), jc.Counts(19, 1, 0, 0, 0), True)
    assert jc.eligible(outside, reading()) and outside.others.errors == 0


def test_a_judge_that_errors_on_the_named_cases_v2_gets_wrong_fixes_nothing(
    tmp_path: Path,
) -> None:
    """The review's case: a claim judge that copies every v2 verdict, and whose replies fail
    twice on 2 of v2's 9 named errors, scored 7 named errors against v2's 9 and was frozen."""
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    wrong = [a for a in answers if a.named and a.v2_passed != a.supported]
    assert len(wrong) == 9
    copies = {(salt, a.case.id): 90 if a.v2_passed else 30 for salt in jc.SALTS for a in answers}
    broken = {(salt, a.case.id): 2 for salt in jc.SALTS for a in wrong[:2]}  # two false fails
    llm = Judges(answers, scores=copies, broken=broken)
    result = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    named = result.cells[0].reading.named
    assert (named.false_passes, named.false_fails, named.judge_errors) == (6, 1, 2)
    assert named.errors_and_judge_errors == 9 and result.cells[0].eligible
    printed = jc.render_stage_a(result, ledger)
    # FP, FF, JE and errors outside the named cases, then on them, where JE counts
    assert re.search(r"\n  t=50 s=off\s+0\s+0\s+0\s+0\s+6\s+1\s+2\s+9\s+yes", printed)
    assert "1. draft t=50 s=off: 9 errors on the named, judge errors included, 0 outside" in printed
    choice = jc.choose(
        ["draft:50:off", "draft:50:90"],
        answers,
        llm,
        claim_config(),
        v2_config(),
        ledger=ledger,
        out_dir=out,
    )
    assert choice.chosen is not None and choice.stop is not None
    assert "no fewer errors than v2" in choice.stop
    held = json.loads(choice.path.read_text(encoding="utf-8"))
    assert held["frozen"] is None
    assert held["candidates"][0]["named"]["means"]["errors_and_judge_errors"] == 9
    assert held["candidates"][0]["events"]["judge_errors"] == 6  # 2 a pass, over three passes
    assert "STOP: the chosen cell makes no fewer errors" in jc.render_choice(choice, ledger)


def test_an_answer_to_keep_must_pass_on_two_of_three_passes() -> None:
    answers = tuning()
    keep = jc.to_keep(answers)[0].key
    stored = jc.stored_v2(answers)

    def passes(*verdicts: bool | None) -> list[dict[str, bool | None]]:
        return [{**stored, keep: v} for v in verdicts]

    assert jc.keeps(answers, passes(True)) and not jc.keeps(answers, passes(False))
    assert jc.keeps(answers, passes(True, False, True))
    assert not jc.keeps(answers, passes(True, False, False))
    assert jc.keeps(answers, passes(None, True, True))
    assert not jc.keeps(answers, passes(True, None, False))  # a judge error is no pass


def test_the_choice_is_the_better_candidate_on_its_means_or_the_round_stops() -> None:
    v2 = reading(named=27, passes=3)  # 9 errors on the named cases a pass

    def judged(t: int, r: Any) -> Any:
        return replace(candidate(r, t), eligible=jc.eligible(r, v2))

    fewer = judged(70, reading(named=12, passes=3))
    more = judged(50, reading(named=15, passes=3))
    assert jc.decide([more, fewer], v2) == ([fewer, more], fewer, None)
    loose = judged(70, reading(7, named=12, passes=3))  # first in order, but 7/3 errors
    assert jc.decide([more, loose], v2)[1:] == (more, None)
    lost = judged(50, reading(named=12, passes=3, keeps=False))
    _, chosen, stop = jc.decide([lost, loose], v2)
    assert chosen is None and stop is not None and "no candidate is eligible" in stop
    level = judged(50, reading(named=27, passes=3))  # fixes none of the named, on its means
    _, chosen, stop = jc.decide([level], v2)
    assert chosen is level and stop is not None and "no fewer errors than v2" in stop
    assert jc.decide([judged(50, reading(named=26, passes=3))], v2)[2] is None


def test_a_step_whose_estimate_would_pass_the_cap_is_refused_before_any_call(
    tmp_path: Path,
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Judges(answers)
    tight = SpendLedger(out / "spend.json", cap_usd=1.0)
    with pytest.raises(SpendCapError, match=r"pilot: its estimate, \$1\.20"):  # 60 at $0.02
        jc.pilot(answers, llm, claim_config(), ledger=tight, out_dir=out)
    pilot_done(out)
    tight = SpendLedger(out / "spend.json", cap_usd=2.0)
    with pytest.raises(SpendCapError, match=r"stage-a draft: its estimate, \$2\.50"):  # 125
        jc.stage_a("draft", answers, llm, claim_config(), ledger=tight, out_dir=out)
    assert llm.requests == [] and not (out / "spend.json").exists()
    assert not (out / "stage-a-draft.json").exists()
    ledger = SpendLedger(out / "spend.json", cap_usd=10.0)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    asked = len(llm.requests)
    # Two more passes of the draft and two of v2: 500 answers at $0.02, past what is left.
    with pytest.raises(SpendCapError, match=r"choose draft:50:off draft:50:90: its estimate, \$10"):
        jc.choose(
            ["draft:50:off", "draft:50:90"],
            answers,
            llm,
            claim_config(),
            v2_config(),
            ledger=ledger,
            out_dir=out,
        )
    assert len(llm.requests) == asked and len(ledger.entries()) == 1


def test_a_step_stops_after_the_call_that_spends_what_the_cap_leaves(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out, 0.0001)  # an estimate of $0.0125 for the 125 answers, which fits
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json", cap_usd=50 * CALL_USD)  # room for 50 calls
    with pytest.raises(
        SpendCapError, match=r"stage-a draft: it has spent \$0\.0200, all that the round's \$0\.02"
    ):
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert len(llm.requests) == 50  # the call that spent the rest was the last one sent
    [entry] = ledger.entries()
    assert (entry.step, entry.uncached_calls) == ("stage-a draft (stopped early)", 50)
    assert entry.usd == pytest.approx(50 * CALL_USD)
    assert not (out / "stage-a-draft.json").exists()
    with pytest.raises(SpendCapError, match="past the"):  # and the next step is refused
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)


def test_the_pilot_and_choose_stop_too_once_they_have_spent_what_the_cap_leaves(
    tmp_path: Path,
) -> None:
    # Priced at $0.40 a call; pricing is not part of the claim judge's version.
    model = {"name": "claude-sonnet-5-5", "max_tokens": 8192}
    dear = claim_config(model=model | {"pricing": {"input_per_mtok": 2e3, "output_per_mtok": 1e4}})
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json", cap_usd=1.20)  # just the pilot's estimate
    with pytest.raises(SpendCapError, match=r"pilot: it has spent \$1\.2000"):
        jc.pilot(answers, llm, dear, ledger=ledger, out_dir=out)
    assert len(llm.requests) == 3 and not (out / "pilot.json").exists()  # so it may run again
    assert [(e.step, e.uncached_calls) for e in ledger.entries()] == [("pilot (stopped early)", 3)]
    out = tmp_path / "again"
    pilot_done(out, 0.0001)
    ledger = SpendLedger(out / "spend.json", cap_usd=159 * CALL_USD + 0.80)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    asked = len(llm.requests)
    with pytest.raises(SpendCapError, match=r"choose draft:50:off draft:50:90: it has spent"):
        jc.choose(
            ["draft:50:off", "draft:50:90"],
            answers,
            llm,
            dear,
            v2_config(),
            ledger=ledger,
            out_dir=out,
        )
    assert len(llm.requests) - asked == 2  # $0.80 left: two calls, and the second was the last
    assert ledger.entries()[-1].step == "choose draft:50:off draft:50:90 (stopped early)"
    assert not (out / "stage-a-draft.judge-s2.json").exists()
    assert not (out / "stage-a-choice.json").exists()


def test_each_paid_step_records_only_what_its_uncached_calls_cost(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Cached(Judges(answers))
    ledger = SpendLedger(out / "spend.json")
    jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    pilot, stage = ledger.entries()
    # Each pilot salt: 20 tables, and a skeptic for each drawn answer labeled supported.
    drawn = jc.pilot_draw(answers)
    per_salt = len(drawn) + sum(a.supported for a in drawn)
    assert (pilot.step, pilot.calls, pilot.uncached_calls) == ("pilot", 3 * per_salt, 3 * per_salt)
    # Stage A: 125 tables and 34 skeptics, of which the pilot's first salt asked some already.
    assert (stage.step, stage.calls, stage.uncached_calls) == ("stage-a draft", 159, 159 - per_salt)
    assert stage.usd == pytest.approx((159 - per_salt) * CALL_USD)
    assert ledger.total_usd() == pytest.approx((3 * per_salt + 159 - per_salt) * CALL_USD)


def test_a_step_that_breaks_off_still_records_what_it_spent(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers, down={answers[10].case.id})
    with pytest.raises(RuntimeError, match="the API is down"):
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    [entry] = ledger.entries()
    assert entry.step == "stage-a draft (stopped early)" and entry.calls == len(llm.requests) > 0
    assert entry.usd == pytest.approx(entry.calls * CALL_USD)
    assert not (out / "stage-a-draft.json").exists()  # a rerun judges again, from the cache


def test_the_pilot_draws_7_7_and_6_answers_with_a_fixed_seed_and_runs_once(
    tmp_path: Path,
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    drawn = jc.pilot_draw(answers)
    assert [sum(a.run_id == run for a in drawn) for run in jc.RUNS] == [7, 7, 6]
    assert jc.pilot_draw(list(reversed(answers))) == drawn  # the order of the set is no part of it

    def hashed(run: str) -> list[Any]:
        key = "judge-v5-pilot/{}/{}"
        on_run = [a for a in answers if a.run_id == run]
        return sorted(
            on_run, key=lambda a: hashlib.sha256(key.format(run, a.case.id).encode()).hexdigest()
        )

    assert drawn == hashed(jc.RUNS[0])[:7] + hashed(jc.RUNS[1])[:7] + hashed(jc.RUNS[2])[:6]
    assert drawn != answers[:20] and any(a.named for a in drawn)
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json")
    result = jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert result.drawn == [a.key for a in drawn]
    assert {s.split("#")[2] for s in llm.salts} == {a.case.id for a in drawn}
    held = json.loads(result.path.read_text(encoding="utf-8"))
    assert held["answers"] == result.drawn and held["draw"]["seed"] == "judge-v5-pilot"
    assert held["draw"]["per_run"] == dict(zip(jc.RUNS, (7, 7, 6), strict=True))
    asked = len(llm.requests)
    with pytest.raises(jc.Refused, match="the pilot runs once"):
        jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert len(llm.requests) == asked and len(ledger.entries()) == 1
    assert json.loads(result.path.read_text(encoding="utf-8")) == held  # what it recorded stands


def test_the_pilot_measures_cost_flips_retries_judge_errors_and_dropped_spans(
    tmp_path: Path,
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    drawn = jc.pilot_draw(answers)
    first, second, third = [a.case.id for a in drawn if a.supported][:3]
    llm = Judges(
        answers,
        scores={("judge-s2", first): 60},  # fails at t = 70 and 85 under one salt
        searches={("judge-s3", second): 80},  # fails at s = 70 under one salt
        broken={("judge", third): 1, ("judge-s2", third): 2},  # retried; then a judge error
        dropped={("judge", first)},
    )
    result = jc.pilot(answers, llm, claim_config(), ledger=SpendLedger(out / "s.json"), out_dir=out)
    assert len(result.drawn) == 20 and not result.stop
    assert result.cost_per_answer == pytest.approx(len(llm.requests) * CALL_USD / 60)
    assert {s.split("#")[0] for s in llm.salts} == set(jc.SALTS)
    of = 19  # the answer with a judge error has no verdict under every salt
    assert result.flips == {
        "t=50 s=off": (0, of),
        "t=50 s=70": (1, of),
        "t=50 s=90": (0, of),
        "t=70 s=off": (1, of),
        "t=70 s=70": (2, of),
        "t=70 s=90": (1, of),
        "t=85 s=off": (1, of),
        "t=85 s=70": (2, of),
        "t=85 s=90": (1, of),
    }
    assert result.dropped_span_flips == {name: (0, 59) for name in CELL_NAMES}  # it still holds
    e = result.events
    assert (e.judgings, e.retried, e.judge_errors) == (60, 2, 1)
    assert (e.dropped, e.with_dropped, e.support_entries, e.search_missing) == (1, 1, 60, 0)
    held = json.loads(result.path.read_text(encoding="utf-8"))
    assert held["cost_per_answer_usd"] == pytest.approx(result.cost_per_answer)
    assert set(held["assessments"]) == set(jc.SALTS) and held["flips"]["t=70 s=70"]["flipped"] == 2
    assert jc.pilot_cost(out) == pytest.approx(result.cost_per_answer)  # what later steps use


def test_the_pilot_reports_the_verdicts_a_dropped_span_flips_at_each_cell(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    lost = next(a for a in jc.pilot_draw(answers) if a.supported)
    # Under one salt its claim keeps no support entry; with the entry back it passes everywhere.
    llm = Judges(answers, misquoted={("judge-s3", lost.case.id)})
    ledger = SpendLedger(out / "spend.json")
    result = jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert result.dropped_span_flips == {name: (1, 60) for name in CELL_NAMES}
    held = json.loads(result.path.read_text(encoding="utf-8"))
    assert held["dropped_span_flips"]["t=85 s=70"] == {"flipped": 1, "of": 60}
    printed = jc.render_pilot(result, ledger)
    flips = printed.split("dropped-span flips: judgings that fail only because the span check")[1]
    assert "  t=85   s=off: 1 of 60   s=70: 1 of 60   s=90: 1 of 60" in flips
    assert "20 tuning answers drawn with the seed 'judge-v5-pilot', 7, 7, 6" in printed


def test_stage_a_waits_for_the_pilot_and_stops_after_an_expensive_one(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json")
    # The refusals are the spend ledger's (agent_evals.spend), which evals judge applies too.
    with pytest.raises(PilotRefusal, match="the pilot comes first"):
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    pilot_done(out, 0.0301)  # over 1.5 times the $0.02 estimate
    with pytest.raises(PilotRefusal, match=r"more than 1\.5 times"):
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    held = json.loads((out / "pilot.json").read_text(encoding="utf-8"))
    stopped = held | {"cost_per_answer_usd": 0.0299, "stop": True}
    (out / "pilot.json").write_text(json.dumps(stopped), encoding="utf-8")
    with pytest.raises(PilotRefusal, match="the pilot recorded a stop"):  # a recorded stop stands
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert llm.requests == []
    pilot_done(out, 0.0299)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert [e.step for e in ledger.entries()] == ["stage-a draft"]
    cells = {name: v for name, v in held["versions"].items() if name != "t=85 s=90"}
    for record in ({"cost_per_answer_usd": 0.02}, held | {"versions": cells}):  # none, 8 of 9
        (out / "pilot.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(jc.Refused, match="does not record the version the pilot judged"):
            jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)


def test_stage_a_counts_the_pilots_version_as_the_first_draft(tmp_path: Path) -> None:
    """The review's case: the pilot judged the first draft, then four other versions were each
    judged on the 125, five in all, and none was refused. The pilot's version counts among the
    four, so the fourth other version is refused, and each other version is a revision."""
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json")
    jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)  # on the first draft
    model = {"name": "claude-sonnet-5-5", "max_tokens": 4096, "pricing": PRICING}
    revisions = [
        claim_config(rubric_version="v4"),
        claim_config(strict_tools=False),
        claim_config(model=model),
    ]
    for n, revision in enumerate(revisions, 1):
        result = jc.stage_a(f"rev{n}", answers, llm, revision, ledger=ledger, out_dir=out)
        assert result.spent is not None
        assert (result.first_draft, result.versions_counted) == (False, n + 1)
        printed = jc.render_stage_a(result, ledger)
        assert "This is a revision: the pilot judged another version, the first draft" in printed
        assert f"Versions so far, the first draft included: {n + 1} of the 4 versions" in printed
    asked, entries = len(llm.requests), len(ledger.entries())
    fifth = claim_config(rubric_version="v4", strict_tools=False)
    with pytest.raises(
        jc.Refused,
        match=r"4 versions are judged already, the first draft the pilot judged counted among "
        r"them.*another is a new proposal",
    ):
        jc.stage_a("rev4", answers, llm, fifth, ledger=ledger, out_dir=out)
    assert len(llm.requests) == asked and len(ledger.entries()) == entries  # no call, no spend
    assert not (out / "stage-a-rev4.json").exists()
    # The first draft itself may still be judged once on the 125: it is no new version.
    draft = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert draft.spent is not None and (draft.first_draft, draft.versions_counted) == (True, 4)
    assert (
        "This is the first draft, the version the pilot judged. Versions so far: 4 of the 4 "
        "versions the design allows (the first draft and three revisions)."
    ) in jc.render_stage_a(draft, ledger)
    again = jc.stage_a("rev1", answers, llm, revisions[0], ledger=ledger, out_dir=out)
    assert again.spent is None and (again.first_draft, again.versions_counted) == (False, 4)


def test_stage_a_judges_no_new_version_once_the_choice_is_made(tmp_path: Path) -> None:
    """The review's case: after choose froze a cell, a new version was still judged, and its pass
    was paid for and added to the candidate order. Refused now, before any call."""
    answers, out = tuning(), tmp_path / "judge-v5"
    llm = Judges(answers)
    ledger = SpendLedger(out / "spend.json")
    jc.pilot(answers, llm, claim_config(), ledger=ledger, out_dir=out)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    specs = ["draft:50:off", "draft:50:90"]
    choice = jc.choose(specs, answers, llm, claim_config(), v2_config(), ledger=ledger, out_dir=out)
    assert choice.chosen is not None and choice.stop is None
    asked, steps = len(llm.requests), [e.step for e in ledger.entries()]
    assert steps == ["pilot", "stage-a draft", "choose draft:50:off draft:50:90"]
    with pytest.raises(
        jc.Refused, match=r"stage-a-choice\.json exists: the choice is made, and no version is"
    ):
        jc.stage_a(
            "rev1", answers, llm, claim_config(rubric_version="v4"), ledger=ledger, out_dir=out
        )
    assert len(llm.requests) == asked and [e.step for e in ledger.entries()] == steps
    assert not (out / "stage-a-rev1.json").exists()
    again = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert again.spent is None and again.choice == choice.path  # read back, as before
    printed = jc.render_stage_a(again, ledger)
    assert f"the choice is made (steps 5 and 6), in {choice.path}: no version is judged" in printed
    assert "after the last revision" not in printed


def test_stage_a_judges_each_version_once_and_orders_every_versions_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)

    def stage(label: str, **config: Any) -> Any:
        return jc.stage_a(label, answers, llm, claim_config(**config), ledger=ledger, out_dir=out)

    draft = stage("draft")
    assert draft.spent is not None and len(draft.cells) == 9
    held = json.loads(draft.path.read_text(encoding="utf-8"))
    assert held["salt"] == "judge" and len(held["assessments"]) == 125 and len(held["grid"]) == 9
    assert len(held["verdicts"]) == 125 and held["events"]["judgings"] == 125
    assert {s.split("#")[0] for s in llm.salts} == {"judge"}
    asked = len(llm.requests)
    again = stage("draft")  # read back, not judged again
    assert again.spent is None and len(llm.requests) == asked and len(ledger.entries()) == 1
    assert [c.reading for c in again.cells] == [c.reading for c in draft.cells]
    with pytest.raises(jc.Refused, match="judged already, as 'draft'"):
        stage("rev1")
    with pytest.raises(jc.Refused, match="another version's pass"):
        stage("draft", rubric_version="v4")
    rev1 = stage("rev1", rubric_version="v4")
    assert len(rev1.order) == 18  # every cell of both versions agrees with every label
    assert [c.spec for c in rev1.order[:4]] == [
        "draft:50:off",
        "rev1:50:off",
        "draft:50:90",
        "rev1:50:90",
    ]
    monkeypatch.setattr(jc, "MAX_VERSIONS", 2)
    with pytest.raises(jc.Refused, match="a new proposal"):
        stage("rev2", strict_tools=False)
    for bad in ("choice", "rev.1", "../rev1", ""):
        with pytest.raises(jc.Refused, match="cannot name a version"):
            stage(bad)
    assert len(ledger.entries()) == 2


def _failing(self: Assessment, t: int, s: int | None) -> ClaimVerdict:
    """A revision's verdict() that fails every answer."""
    return ClaimVerdict(fully_supported=False, core_supported=False, support_score=0.0, reason="r")


def test_each_pass_saves_its_verdicts_and_only_its_own_versions_code_reads_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    others = [a for a in answers if not a.named]
    error, lost = others[0], next(a for a in others[1:] if a.supported)
    llm = Judges(answers, broken={("judge", error.case.id): 2}, misquoted={("judge", lost.case.id)})
    draft = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    held = json.loads(draft.path.read_text(encoding="utf-8"))
    # Every answer's verdicts at the nine cells, as the draft's code read them when judged.
    assert held["verdicts"][error.key] == {"judge_error": True, "cells": {}}
    saved = held["verdicts"][lost.key]["cells"]
    assert set(saved) == CELL_NAMES
    assert all(c["dropped_span_flip"] and not c["fully_supported"] for c in saved.values())
    assert all(set(v["cells"]) == CELL_NAMES for k, v in held["verdicts"].items() if k != error.key)
    assert (held["events"]["judge_errors"], held["events"]["dropped"]) == (1, 1)
    assert all(c.dropped_span_flips == 1 for c in draft.cells)
    with monkeypatch.context() as patched:  # a revision whose verdict() fails every answer
        patched.setattr(Assessment, "verdict", _failing)
        rev1 = claim_config(rubric_version="v4")
        result = jc.stage_a("rev1", answers, llm, rev1, ledger=ledger, out_dir=out)
        assert not any(c.eligible for c in result.cells)  # its own pass, read with its code
        # The draft's cells and their order are its own, as judged: not re-read with rev1's code.
        assert [(c.spec, c.reading) for c in result.order] == [
            (c.spec, c.reading) for c in draft.order
        ]
        with pytest.raises(jc.Refused, match=r"stage-a-draft\.json: the code in place has the"):
            jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    # With the code as it was, rev1's pass reads otherwise on the 33 answers labeled supported
    # that have a verdict: 32 pass now, and the one whose span was dropped fails only by that.
    with pytest.raises(jc.Refused, match=r"stage-a-rev1\.json: .*: 33 answers read otherwise"):
        jc.stage_a(
            "rev1", answers, llm, claim_config(rubric_version="v4"), ledger=ledger, out_dir=out
        )
    again = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert again.spent is None and [c.reading for c in again.cells] == [
        c.reading for c in draft.cells
    ]


def test_a_saved_pass_is_read_only_against_the_tuning_set_it_was_judged_on(
    tmp_path: Path,
) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    relabeled = design_rows()
    relabeled[60] = replace(relabeled[60], supported=True)  # an answer outside the named cases
    changed = jc.load_tuning_set(FakeStore(relabeled), dev_cases(relabeled))
    with pytest.raises(jc.Refused, match="judged on another tuning set"):
        jc.stage_a("draft", changed, llm, claim_config(), ledger=ledger, out_dir=out)
    held = json.loads((out / "stage-a-draft.json").read_text(encoding="utf-8"))
    del held["verdicts"][answers[0].key]["cells"]["t=85 s=90"]
    (out / "stage-a-draft.json").write_text(json.dumps(held), encoding="utf-8")
    with pytest.raises(jc.Refused, match="verdicts at the nine cells"):
        jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)


def test_choose_freezes_the_better_candidate_on_three_pass_means(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    unsure = answers[-1].case.id
    lost = next(a for a in answers if a.supported and not a.named)
    # Under one more salt, v2 gives one reply with no verdict, and gets a false pass right, and
    # the claim judge's span check drops the one support entry of an answer labeled supported.
    llm = Judges(
        answers,
        v2={("judge-s2", unsure): None, ("judge-s2", NAMED[4]): False},
        misquoted={("judge-s2", lost.case.id)},
    )
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    before = len(llm.requests)
    specs = ["draft:50:90", "draft:50:off"]
    result = jc.choose(specs, answers, llm, claim_config(), v2_config(), ledger=ledger, out_dir=out)
    # The draft's two more passes, one each for both cells, and v2's two, on support only.
    assert len(llm.calls("claims", before)) == 250
    v2_salts = llm.calls("supported", before)
    assert len(v2_salts) == 250 and {s.split("#")[0] for s in v2_salts} == {"judge-s2", "judge-s3"}
    assert len(llm.salts) - before == 250 + 250 + 2 * 34  # and the skeptics; nothing else
    for salt in ("judge-s2", "judge-s3"):
        assert (out / f"stage-a-draft.{salt}.json").is_file()
    assert [(c.t, c.s) for c in result.ordered] == [(50, None), (50, 90)]
    assert result.chosen is result.ordered[0] and result.stop is None and result.code_is_chosen
    assert [c.reading.named.passes for c in result.ordered] == [3, 3]
    assert [c.dropped_span_flips for c in result.ordered] == [1, 1]  # over the three passes
    assert result.ordered[0].events.judgings == 375 and result.ordered[0].events.dropped == 1
    v2 = result.v2  # its stored pass, with 9 errors on the named cases, and two more
    assert (v2.named.passes, v2.named.mean(v2.named.errors), v2.others.judge_errors) == (
        3,
        Fraction(26, 3),
        1,
    )
    held = json.loads(result.path.read_text(encoding="utf-8"))
    version = jc.versions(llm, claim_config())["t=50 s=off"]
    assert held["frozen"] == {"label": "draft", "t": 50, "s": None, "version": version}
    assert held["v2"]["named"]["means"]["errors"] == pytest.approx(26 / 3)
    first = held["candidates"][0]
    assert first["dropped_span_flips"] == {"total": 1, "mean": pytest.approx(1 / 3)}
    assert (first["events"]["judgings"], first["events"]["dropped"]) == (375, 1)
    assert first["others"]["totals"]["false_fails"] == 1  # the dropped span's fail
    printed = jc.render_choice(result, ledger)
    assert "draft t=50 s=off, of the 375 judgings: retried 0 of 375 (0%); judge errors" in printed
    assert re.search(r"\n  draft t=50 s=off\s+0\s+0\.33\s+0\s+0\.33\s.*\s0\.33\s+yes", printed)
    entry = ledger.entries()[-1]
    assert entry.step == "choose draft:50:90 draft:50:off"
    assert entry.calls == entry.uncached_calls == len(llm.requests) - before
    with pytest.raises(jc.Refused, match="the choice is made once"):
        jc.choose(specs, answers, llm, claim_config(), v2_config(), ledger=ledger, out_dir=out)


def test_choose_takes_only_the_first_two_of_the_candidate_order(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)

    def choose(*specs: str) -> Any:
        return jc.choose(
            list(specs), answers, llm, claim_config(), v2_config(), ledger=ledger, out_dir=out
        )

    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    before = len(llm.requests)
    for wrong in (
        ("draft:50:off",),
        ("draft:50:off", "draft:50:70"),
        ("draft:50:off", "draft:50:off"),
        ("draft:50:off", "draft:50:90", "draft:50:70"),
    ):
        with pytest.raises(jc.Refused, match="first two of step 4's order: draft:50:off draft"):
            choose(*wrong)
    for bad in ("draft:60:off", "draft:50:80", "draft:50", "draft", "rev1:50:off:x"):
        with pytest.raises(jc.Refused, match="is not LABEL:T:S"):
            choose(bad, "draft:50:90")
    assert len(llm.requests) == before and len(ledger.entries()) == 1


def test_choose_stops_the_round_when_no_cell_of_any_version_is_eligible(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    keep = jc.to_keep(answers)[0].case.id
    llm = Judges(answers, scores={("judge", keep): 30})  # the answer to keep, failed everywhere
    result = jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    assert result.order == [] and not any(c.eligible for c in result.cells)
    with pytest.raises(jc.Refused, match="no cell of any version is eligible"):
        jc.choose(
            ["draft:50:off", "draft:50:90"],
            answers,
            llm,
            claim_config(),
            v2_config(),
            ledger=ledger,
            out_dir=out,
        )


def test_choose_judges_each_candidate_with_its_own_versions_code(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)
    draft, rev1 = claim_config(), claim_config(rubric_version="v4")
    jc.stage_a("draft", answers, llm, draft, ledger=ledger, out_dir=out)
    jc.stage_a("rev1", answers, llm, rev1, ledger=ledger, out_dir=out)
    specs = ["draft:50:off", "rev1:50:off"]  # tied in all but the version: one from each

    def choose(code: JudgeConfig) -> Any:
        return jc.choose(specs, answers, llm, code, v2_config(), ledger=ledger, out_dir=out)

    before = len(llm.requests)
    with pytest.raises(jc.Refused, match="later passes of draft, rev1 need that version's"):
        choose(claim_config(strict_tools=False))  # a third version: nothing it can judge
    assert len(llm.requests) == before and len(ledger.entries()) == 2
    with pytest.raises(jc.Refused, match="later passes of rev1 need that version's"):
        choose(draft)  # the draft's own passes are judged and kept; v2 waits for the last run
    assert len(llm.calls("claims", before)) == 250 and llm.calls("supported", before) == []
    assert (out / "stage-a-draft.judge-s3.json").is_file()
    assert not (out / "stage-a-rev1.judge-s2.json").exists()
    assert not (out / "stage-a-choice.json").exists()
    before = len(llm.requests)
    result = choose(rev1)
    assert len(llm.calls("claims", before)) == 250  # rev1's; the draft's are read back
    assert len(llm.calls("supported", before)) == 250
    assert [c.label for c in result.ordered] == ["draft", "rev1"]
    assert result.chosen is result.ordered[0] and result.stop is None
    assert not result.code_is_chosen  # the draft is frozen while rev1's code is in place
    assert "put that version's graders back" in jc.render_choice(result, ledger)
    step = "choose draft:50:off rev1:50:off"  # the run that judged the draft's passes, then this
    assert [e.step for e in ledger.entries()] == ["stage-a draft", "stage-a rev1", step, step]


def test_choose_reads_another_versions_passes_from_their_saved_verdicts(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)
    draft, rev1 = claim_config(), claim_config(rubric_version="v4")
    jc.stage_a("draft", answers, llm, draft, ledger=ledger, out_dir=out)
    jc.stage_a("rev1", answers, llm, rev1, ledger=ledger, out_dir=out)
    specs = ["draft:50:off", "rev1:50:off"]
    with pytest.raises(jc.Refused, match="later passes of rev1 need that version's"):
        jc.choose(specs, answers, llm, draft, v2_config(), ledger=ledger, out_dir=out)
    # What the draft's passes found, rewritten so that reading it again would fail every answer.
    for path in out.glob("stage-a-draft*.json"):
        held = json.loads(path.read_text(encoding="utf-8"))
        for found in held["assessments"].values():
            for claim in found["claims"]:
                claim["score"] = 0
        path.write_text(json.dumps(held), encoding="utf-8")
    result = jc.choose(specs, answers, llm, rev1, v2_config(), ledger=ledger, out_dir=out)
    # With rev1's code in place, the draft's three passes are read from their saved verdicts.
    assert [(c.label, c.eligible) for c in result.ordered] == [("draft", True), ("rev1", True)]
    assert result.chosen is result.ordered[0] and result.stop is None
    with pytest.raises(jc.Refused, match=r"stage-a-draft\.json: the code in place has the"):
        jc.stage_a("draft", answers, llm, draft, ledger=ledger, out_dir=out)  # its own code


def test_choose_refuses_a_later_pass_of_another_version_before_any_call(tmp_path: Path) -> None:
    answers, out = tuning(), tmp_path / "judge-v5"
    pilot_done(out)
    ledger = SpendLedger(out / "spend.json")
    llm = Judges(answers)
    jc.stage_a("draft", answers, llm, claim_config(), ledger=ledger, out_dir=out)
    first = json.loads((out / "stage-a-draft.json").read_text(encoding="utf-8"))
    other = jc.versions(llm, claim_config(rubric_version="v4"))
    for salt, versions in (("judge-s2", first["versions"]), ("judge-s3", other)):
        later = first | {"salt": salt, "versions": versions}
        (out / f"stage-a-draft.{salt}.json").write_text(json.dumps(later), encoding="utf-8")
    before = len(llm.requests)
    with pytest.raises(jc.Refused, match="the passes of draft are not all one version's"):
        jc.choose(
            ["draft:50:off", "draft:50:90"],
            answers,
            llm,
            claim_config(),
            v2_config(),
            ledger=ledger,
            out_dir=out,
        )
    assert len(llm.requests) == before and len(ledger.entries()) == 1  # v2 was not judged


def test_the_commands_open_the_store_read_only_and_print_what_they_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = design_rows()
    store = FakeStore(rows)
    opened: list[bool] = []

    def open_store(path: Path, *, read_only: bool = False) -> FakeStore:
        opened.append(read_only)
        return store

    suite = tmp_path / "dev.jsonl"
    write_suite(suite, dev_cases(rows).values())
    here = tmp_path.as_posix()
    paths = f'paths:\n  db: "{here}/evals.duckdb"\n  cache_dir: "{here}/cache"\n'
    priced = "    pricing: {input_per_mtok: 2.0, output_per_mtok: 10.0}\n"
    claim_yaml, v2_yaml = tmp_path / "judge-v5.yaml", tmp_path / "default.yaml"
    claim_yaml.write_text(
        paths
        + "judge:\n  method: claims\n  grader_name: judge-v5\n  rubric_version: v5\n"
        + "  responsive: false\n  model:\n    name: claude-sonnet-5-5\n    max_tokens: 8192\n"
        + priced,
        encoding="utf-8",
    )
    v2_yaml.write_text(
        paths + "judge:\n  model:\n    name: claude-sonnet-5-5\n    max_tokens: 2048\n" + priced,
        encoding="utf-8",
    )
    llm = Judges(tuning())
    out = tmp_path / "judge-v5"
    for name, value in (
        ("load_dotenv", lambda: False),
        ("ResultsStore", open_store),
        ("AnthropicClient", lambda *args, **kwargs: llm),
        ("DEV_SUITE", suite),
        ("CLAIM_CONFIG", claim_yaml),
        ("V2_CONFIG", v2_yaml),
        ("OUT_DIR", out),
    ):
        monkeypatch.setattr(jc, name, value)
    runner = CliRunner()
    refused = runner.invoke(jc.app, ["stage-a", "draft"])
    assert refused.exit_code == 2 and "REFUSED: the pilot comes first" in refused.output
    pilot = runner.invoke(jc.app, ["pilot"])
    assert pilot.exit_code == 0, pilot.output
    assert "cost per answer: $0.0005" in pilot.output and "t=70   s=off: 0 of 20" in pilot.output
    assert "dropped-span flips: judgings that fail only because" in pilot.output
    again = runner.invoke(jc.app, ["pilot"])
    assert again.exit_code == 2 and "the pilot runs once" in again.output
    stage = runner.invoke(jc.app, ["stage-a", "draft"])
    assert stage.exit_code == 0, stage.output
    assert "This is the first draft, the version the pilot judged." in stage.output
    assert (
        "1. draft t=50 s=off: 0 errors on the named, judge errors included, 0 outside them"
        "   <- candidate"
    ) in stage.output
    assert "choose draft:50:off draft:50:90" in stage.output
    chosen = runner.invoke(jc.app, ["choose", "draft:50:off", "draft:50:90"])
    assert chosen.exit_code == 0, chosen.output
    assert "FROZEN: draft, t = 50, s = off" in chosen.output
    assert "support_threshold: 50, skeptic_threshold: null" in chosen.output
    assert opened == [True] * 5 and store.closed
    assert [e["step"] for e in json.loads((out / "spend.json").read_text("utf-8"))["entries"]] == [
        "pilot",
        "stage-a draft",
        "choose draft:50:off draft:50:90",
    ]
