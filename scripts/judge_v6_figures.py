"""The figures the judge v6 proposal cites (docs/plans/judge-v6.md), recomputed from the records of
the claim judge's first round in results/judge-v5/ and written to results/judge-v5/analysis/.

    uv run python scripts/judge_v6_figures.py

Nothing is judged anew. Every figure is a re-reading of the round's saved replies (the pilot, the
first draft's pass and revision 1's three passes) with the claim judge's own code: each saved
assessment is parsed by Assessment.model_validate and read at any threshold by Assessment.verdict,
and links and spans are found by the grader's own patterns and functions, so a figure is what the
code does. The one record the pass files do not hold, the per-answer verdicts of v2's two extra
passes from the choice step (salts judge-s2 and judge-s3), is replayed from the call cache: the
exact request JudgeGrader makes from configs/default.yaml with the responsive question off, sent
through a client that reads the cache alone and raises on any miss, so no call reaches the API.
The results database is opened read-only for the 125 tuning answers' stored outputs (the answer
and its citations), nothing else is read from it, and it is closed before anything else happens;
the labels and v2's stored verdicts come from the pass files.

The script prints the figures and writes two files: figures.json, each figure with its scope
stated, and v2-extra-passes.json, v2's replayed verdicts per answer and salt. Two rules on the
blind classes of the judge's reasons (analysis/classes.json, read and never written) are simulated
on revision 1's three passes at t = 70 with s off and written beside the figures with their texts:
the classes decide nothing but what the simulation says they would have. The named-case lists
are v5's (scripts/judge_compare_v5.py, frozen 2026-10-28) and v6's, which adds the five cases the
proposal was written from; both splits of the tuning set are reported, v5's (106 answers outside
the named cases, 19 named) and v6's (93 and 32). The grid under test is t in {64, 69, 74} with s
in {off, 49, 59}; t = 70 with s off, revision 1's candidate cell, is reported beside it for
continuity. Run from the repository root.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import duckdb
import judge_compare_v5 as v5

from agent_evals.config import load_config
from agent_evals.graders.claim_judge import (
    _LINK,
    _URL,
    Assessment,
    _occurs,
    normalize,
    without_links,
)
from agent_evals.graders.claim_judge import S_GRID as V5_S_GRID
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.cache import CallCache
from agent_evals.harness.llm import LLMResponse
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.spend import DEFAULT_PATH, PILOT_FILE
from agent_evals.suites import load_suite
from agent_evals.types import AgentResult, Case, Usage

RESULTS = v5.OUT_DIR  # results/judge-v5
OUT_DIR = RESULTS / "analysis"
FIGURES = OUT_DIR / "figures.json"
V2_PASSES = OUT_DIR / "v2-extra-passes.json"
SNAPSHOT_PAGES = Path("data/snapshots/aws-docs/pages.jsonl")
# The cases the proposal was written from, which join v5's list (docs/plans/judge-v6.md, appendix).
ADDED_CASES = frozenset(
    {"docs-ai-003-p1", "docs-ai-003-p3", "docs-chg-010", "docs-mix-042", "docs-mix-058"}
)
NAMED_V6 = v5.NAMED_CASES | ADDED_CASES
# The grid under test, and revision 1's candidate cell for continuity.
T_GRID = (64, 69, 74)
S_GRID: tuple[int | None, ...] = (None, 49, 59)
CONTINUITY: tuple[int, int | None] = (70, None)
CELLS: tuple[tuple[int, int | None], ...] = (*((t, s) for t in T_GRID for s in S_GRID), CONTINUITY)
# Revision 1's three passes, by salt, and the first draft's one.
REV1 = "rev1"
DRAFT = "draft"
REV1_SALTS = v5.SALTS
V2_EXTRA_SALTS = v5.SALTS[1:]
# The four answers behind v5's stop, by run (docs/plans/judge-v6.md, "What v5's records show").
CHAMPION, ITERATION4 = v5.RUNS[1], v5.RUNS[2]
FOUR = (
    f"{CHAMPION}/docs-chg-010",
    f"{ITERATION4}/docs-mix-042",
    f"{CHAMPION}/docs-ai-003-p3",
    f"{ITERATION4}/docs-mix-058",
)
# The fixes the proposal claims on the named cases, as judgings over three passes: the build check
# it projects takes them off revision 1's errors (figure i, labeled fitted).
FITTED_FIXES = {FOUR[0]: 3, FOUR[1]: 1, FOUR[2]: 2}
OWN_NAME = "Bedrock"  # the cited service's name flagged inside docs-ai-003-p3's words of no claim
SCORE_BAND = (50, 85)  # the band of claim scores that decides verdicts
WEAKEST_LEVELS = (65, 70, 72, 75)
T_SWEEP = range(50, 91)
S_SWEEP = (*range(30, 91, 5), 49, 59)
YES_CELL = "yes"  # a quota table's adjustable column, whose Yes links to the console
# The blind classes of the judge's reasons, and the two rules simulated on them at revision 1's
# candidate cell (docs/plans/judge-v6.md, "A rule on two of those types"): the first reader's
# classes, the ones given in every batch. The two-type rule fails a name or term in no quote and a
# headerless table cell; the categories-only rule passes stated and plain rewording alone.
CLASSES = OUT_DIR / "classes.json"
RULE_CELL: tuple[int, int | None] = CONTINUITY
READER = "reader_1"
TERM_IN_NO_QUOTE = "NAME"
OTHER = "OTHER"
HEADERLESS_CELL = "a headerless table cell"  # the OTHER kind the two-type rule fails
PASSING_CLASSES = frozenset({"ST", "RW"})
BATCH_1 = 1  # the weakest claims scored 50 to 85, every claim tied at its judging's lowest score

Verdicts = dict[str, bool]


class Refused(Exception):
    """A record is not what the round's design says it is: the figures are not computed."""


@dataclass(frozen=True)
class Note:
    """A tuning answer as the pass files record it, with its place in both named lists."""

    key: str
    run_id: str
    case_id: str
    supported: bool
    v2_passed: bool

    @property
    def named_v5(self) -> bool:
        return self.case_id in v5.NAMED_CASES

    @property
    def named_v6(self) -> bool:
        return self.case_id in NAMED_V6

    def named(self, split: str) -> bool:
        return self.named_v5 if split == "v5" else self.named_v6


@dataclass(frozen=True)
class PassRecord:
    """One saved pass: its file, salt, assessments parsed by the code in place, and its usage."""

    label: str
    salt: str
    path: Path
    assessments: dict[str, Assessment]
    usage: Usage
    events: dict[str, Any]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def cell(t: int, s: int | None) -> str:
    return v5.cell_name(t, s)


def fully(found: Assessment, t: int, s: int | None) -> bool:
    return found.verdict(t, s).fully_supported


def weakest(found: Assessment) -> int | None:
    """The lowest claim score of a judging, or None with no claims."""
    return min((claim.score for claim in found.claims), default=None)


# ---- the records -----------------------------------------------------------------------------


def load_notes(raw: Mapping[str, Any]) -> dict[str, Note]:
    notes: dict[str, Note] = {}
    for entry in raw["answers"]:
        run_id, _, case_id = str(entry["key"]).partition("/")
        note = Note(
            key=str(entry["key"]),
            run_id=run_id,
            case_id=case_id,
            supported=bool(entry["supported"]),
            v2_passed=bool(entry["v2_passed"]),
        )
        if note.named_v5 != bool(entry["named"]):
            raise Refused(f"{note.key}: the pass file's named flag is not v5's list")
        notes[note.key] = note
    if len(notes) != v5.TUNING_ANSWERS:
        raise Refused(f"the pass file holds {len(notes)} answers, not {v5.TUNING_ANSWERS}")
    return notes


def load_pass(label: str, salt: str, notes: Mapping[str, Note]) -> PassRecord:
    """A saved pass, parsed by the claim judge's code in place, which must read every saved
    verdict as it was read when the pass was judged."""
    path = v5.pass_path(RESULTS, label, salt)
    raw = load_json(path)
    if (raw["label"], raw["salt"]) != (label, salt):
        raise Refused(f"{path} holds {raw['label']!r} under salt {raw['salt']!r}")
    if load_notes(raw) != dict(notes):
        raise Refused(f"{path} was judged on another tuning set than revision 1's first pass")
    assessments = {key: Assessment.model_validate(a) for key, a in raw["assessments"].items()}
    if set(assessments) != set(notes):
        raise Refused(f"{path} does not hold every tuning answer's assessment")
    for key, found in assessments.items():
        saved = v5.AnswerVerdicts.model_validate(raw["verdicts"][key])
        if v5.read_verdicts(found) != saved:
            raise Refused(f"{path}: the code in place reads {key} otherwise than it was read")
    return PassRecord(
        label=label,
        salt=salt,
        path=path,
        assessments=assessments,
        usage=Usage.model_validate(raw["usage"]),
        events=dict(raw["events"]),
    )


def load_pilot(draft: PassRecord) -> tuple[dict[str, Any], dict[str, dict[str, Assessment]]]:
    """The pilot's record and its assessments by salt. The pilot judged the first draft, so its
    pass under the salt `judge` is the draft's pass on the same 20 answers."""
    raw = load_json(RESULTS / PILOT_FILE)
    draft_raw = load_json(draft.path)
    if raw["versions"] != draft_raw["versions"]:
        raise Refused("the pilot did not judge the first draft's version")
    passes = {
        str(salt): {key: Assessment.model_validate(a) for key, a in found.items()}
        for salt, found in raw["assessments"].items()
    }
    repeated = passes[REV1_SALTS[0]]
    if any(repeated[key] != draft.assessments[key] for key in repeated):
        raise Refused("the pilot's pass under the salt 'judge' differs from the draft's")
    return raw, passes


def stored_outputs(db: Path, notes: Mapping[str, Note]) -> dict[str, dict[str, Any]]:
    """The 125 tuning answers' stored outputs (the answer and its citations) and nothing else,
    from the results database opened read-only and closed before anything else happens. Each run
    must be a dev run, never an online reference run."""
    outputs: dict[str, dict[str, Any]] = {}
    con = duckdb.connect(str(db), read_only=True)
    try:
        for run_id in v5.RUNS:
            row = con.execute(
                "SELECT COALESCE(online, FALSE) FROM runs WHERE run_id = ?", [run_id]
            ).fetchone()
            if row is None or bool(row[0]):
                raise Refused(f"{run_id} is not a stored dev run")
            wanted = sorted(n.case_id for n in notes.values() if n.run_id == run_id)
            marks = ", ".join("?" for _ in wanted)
            rows = con.execute(
                "SELECT case_id, output FROM attempts WHERE run_id = ? AND attempt = 1 "
                f"AND case_id IN ({marks})",
                [run_id, *wanted],
            ).fetchall()
            for case_id, output in rows:
                outputs[f"{run_id}/{case_id}"] = json.loads(output)
    finally:
        con.close()
    if set(outputs) != set(notes):
        raise Refused("the results database does not hold every tuning answer's attempt 1")
    return outputs


# ---- v2's two extra passes, replayed from the call cache -------------------------------------


class CacheMiss(RuntimeError):
    """A request whose reply is not in the call cache: the replay stops, and nothing is sent."""


class CacheOnlyClient:
    """An LLMClient that answers from the call cache alone. Every request is keyed as
    AnthropicClient keys it, so a cached reply is the one the API gave that request under that
    salt; a request with no cached reply raises CacheMiss, and no call is ever sent."""

    def __init__(self, cache: CallCache) -> None:
        self.cache = cache
        self.hits = 0

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        key = CallCache.key(request, salt)
        hit = self.cache.get(key)
        if hit is None:
            raise CacheMiss(f"no cached reply for salt {salt!r}: the replay reads the cache only")
        self.hits += 1
        return LLMResponse.model_validate({**hit, "cache_hit": True, "latency_ms": 0.0})


@dataclass(frozen=True)
class V2Replay:
    verdicts: dict[str, dict[str, bool | None]]  # salt -> key -> judge_supported; None: no verdict
    records: list[dict[str, Any]]  # per answer and salt, for v2-extra-passes.json
    stored_salt: dict[str, Any]  # the stored pass's salt replayed, checked against v2_passed
    usage: Usage  # the extra passes' calls, from the cached replies
    cost_per_answer_usd: float | None  # at the v2 config's pricing


def replay_v2(
    cases: Mapping[str, Case], outputs: Mapping[str, dict[str, Any]], notes: Mapping[str, Note]
) -> V2Replay:
    """v2's two extra passes, replayed: the request JudgeGrader makes from configs/default.yaml
    with the responsive question off, under the choice step's salts, answered from the call cache
    alone. The stored pass's salt is replayed too and checked against the stored verdicts, which
    shows that the request built here is the one the stored verdicts came from."""
    config = load_config(v5.V2_CONFIG)
    judge = config.judge.model_copy(update={"responsive": False})
    client = CacheOnlyClient(CallCache(config.paths.cache_dir))
    verdicts: dict[str, dict[str, bool | None]] = {}
    records: list[dict[str, Any]] = []
    usage = Usage()
    for salt in V2_EXTRA_SALTS:
        grader = JudgeGrader(client, judge, salt=salt)
        if grader.version != ONLINE_JUDGE_VERSION:
            raise Refused(f"the v2 config's judge is {grader.version}, not {ONLINE_JUDGE_VERSION}")
        verdicts[salt] = {}
        for key, note in notes.items():
            grades = _v2_grades(grader, cases[note.case_id], outputs[key])
            verdicts[salt][key] = grades.get("judge_supported")
            record: dict[str, Any] = {"key": key, "salt": salt}
            record["judge_supported"] = grades.get("judge_supported")
            if "judge_core_supported" in grades:
                record["judge_core_supported"] = grades["judge_core_supported"]
            records.append(record)
        usage = usage.add(grader.usage)
    # The stored pass, under its own salt, replayed as a check of the request: a soft check, since
    # it is no figure, but every reply it finds must agree with the stored verdict.
    grader = JudgeGrader(client, judge, salt=REV1_SALTS[0])
    agree = misses = 0
    for key, note in notes.items():
        try:
            grades = _v2_grades(grader, cases[note.case_id], outputs[key])
        except CacheMiss:
            misses += 1
            continue
        if grades.get("judge_supported") != note.v2_passed:
            raise Refused(
                f"{key}: the cached reply under the stored salt is not the stored verdict"
            )
        agree += 1
    stored_salt = {"salt": REV1_SALTS[0], "replies_in_cache": agree, "misses": misses}
    cost = usage.cost_usd(judge.model.pricing)
    per_answer = None if cost is None else cost / (len(notes) * len(V2_EXTRA_SALTS))
    return V2Replay(verdicts, records, stored_salt, usage, per_answer)


def _v2_grades(grader: JudgeGrader, case: Case, output: dict[str, Any]) -> dict[str, bool | None]:
    """v2's verdicts on one answer, by metric, as judge_compare_v5's choice step read them: a
    reply without a valid verdict is None, and a cache miss raises."""
    try:
        grades = grader.grade(case, AgentResult(output=output))
    except ValueError:
        return {"judge_supported": None, "judge_core_supported": None}
    return {g.metric: g.passed for g in grades}


# ---- counting -------------------------------------------------------------------------------


def errors(
    verdicts: Mapping[str, bool | None], notes: Mapping[str, Note], keys: Iterable[str]
) -> tuple[int, int]:
    """False passes and false fails of one pass on these answers; a None verdict is neither."""
    false_passes = false_fails = 0
    for key in keys:
        verdict = verdicts[key]
        if verdict is None:
            continue
        if verdict and not notes[key].supported:
            false_passes += 1
        elif not verdict and notes[key].supported:
            false_fails += 1
    return false_passes, false_fails


def reading(
    passes: Sequence[tuple[str, Mapping[str, bool | None]]],
    notes: Mapping[str, Note],
    keys: Sequence[str],
) -> dict[str, Any]:
    """A judge's errors on these answers, per pass and summed, with the means over its passes."""
    per_pass: list[dict[str, Any]] = []
    for salt, verdicts in passes:
        fp, ff = errors(verdicts, notes, keys)
        per_pass.append({"salt": salt, "false_passes": fp, "false_fails": ff, "errors": fp + ff})
    totals = {
        name: sum(int(p[name]) for p in per_pass)
        for name in ("false_passes", "false_fails", "errors")
    }
    return {
        "answers": len(keys),
        "passes": len(passes),
        "per_pass": per_pass,
        "totals": totals,
        "means": {name: _mean(total, len(passes)) for name, total in totals.items()},
    }


def _mean(total: int, passes: int) -> float:
    return round(float(Fraction(total, passes)), 4) if passes else 0.0


def side(notes: Mapping[str, Note], split: str, named: bool) -> list[str]:
    """The answers on one side of a split: named under that list, or outside it."""
    return [key for key, note in notes.items() if note.named(split) == named]


def kept(notes: Mapping[str, Note], split: str) -> list[str]:
    """The kept set: named, labeled supported and passed by v2's stored verdict."""
    return [k for k, n in notes.items() if n.named(split) and n.supported and n.v2_passed]


class Rev1:
    """Revision 1's three passes, with their verdicts at any cell read once and kept."""

    def __init__(self, passes: Sequence[PassRecord]) -> None:
        self.passes = list(passes)
        self._read: dict[tuple[str, int, int | None], Verdicts] = {}

    @property
    def salts(self) -> list[str]:
        return [p.salt for p in self.passes]

    def judgings(self) -> Iterable[tuple[str, str, Assessment]]:
        for p in self.passes:
            for key, found in p.assessments.items():
                yield p.salt, key, found

    def verdicts(self, salt: str, t: int, s: int | None) -> Verdicts:
        at = (salt, t, s)
        if at not in self._read:
            found = next(p for p in self.passes if p.salt == salt).assessments
            self._read[at] = {key: fully(a, t, s) for key, a in found.items()}
        return self._read[at]

    def all_passes(self, t: int, s: int | None) -> list[tuple[str, Mapping[str, bool | None]]]:
        return [(salt, self.verdicts(salt, t, s)) for salt in self.salts]

    def passes_of(self, key: str, t: int, s: int | None) -> int:
        """How many of the three passes pass this answer at the cell."""
        return sum(self.verdicts(salt, t, s)[key] for salt in self.salts)


# ---- the figures ----------------------------------------------------------------------------


def figure_a(notes: Mapping[str, Note], rev1: Rev1) -> dict[str, Any]:
    """The tuning set split under both named lists, and the kept set."""
    stored = {key: note.v2_passed for key, note in notes.items()}
    splits: dict[str, Any] = {}
    for split in ("v5", "v6"):
        sides: dict[str, Any] = {}
        for name, named in (("outside_named", False), ("named", True)):
            keys = side(notes, split, named)
            fp, ff = errors(stored, notes, keys)
            sides[name] = {
                "answers": len(keys),
                "labeled_supported": sum(notes[k].supported for k in keys),
                "v2_stored_false_passes": fp,
                "v2_stored_false_fails": ff,
            }
        kept_keys = kept(notes, split)
        sides["kept"] = {
            "answers": len(kept_keys),
            "keys": kept_keys,
            "rev1_passes_of_three": {
                key: {cell(t, s): rev1.passes_of(key, t, s) for t, s in CELLS} for key in kept_keys
            },
        }
        splits[split] = sides
    moved = sorted(k for k, n in notes.items() if n.named_v6 and not n.named_v5)
    return {
        "scope": (
            "The 125 tuning answers as the pass files record them, under v5's named list and v6's "
            "(v5's plus the five added cases). The kept set is the named answers labeled "
            "supported that v2's stored verdict passes; for each, how many of revision 1's three "
            "passes pass it at each cell of the grid under test and at t = 70 with s off."
        ),
        "named_cases": {
            "v5": sorted(v5.NAMED_CASES),
            "added_for_v6": sorted(ADDED_CASES),
            "v6": sorted(NAMED_V6),
        },
        "answers_moved": {"count": len(moved), "keys": moved},
        "splits": splits,
    }


def figure_b(
    notes: Mapping[str, Note], replay: V2Replay, choice: Mapping[str, Any]
) -> dict[str, Any]:
    """v2's three passes: the stored verdicts and the two replayed, against the choice record."""
    stored: dict[str, bool | None] = {key: note.v2_passed for key, note in notes.items()}
    passes: list[tuple[str, Mapping[str, bool | None]]] = [(REV1_SALTS[0], stored)]
    passes += [(salt, replay.verdicts[salt]) for salt in V2_EXTRA_SALTS]
    by_side: dict[str, Any] = {}
    for split in ("v5", "v6"):
        for name, named in (("outside_named", False), ("named", True)):
            keys = side(notes, split, named)
            by_side[f"{split}_{name}"] = reading(passes, notes, keys)
    # The totals must be the choice record's, which counted the same three passes.
    recorded = choice["v2"]
    for name, found in (("others", by_side["v5_outside_named"]), ("named", by_side["v5_named"])):
        want = recorded[name]["totals"]
        got = found["totals"]
        if (want["false_passes"], want["false_fails"]) != (got["false_passes"], got["false_fails"]):
            raise Refused(
                f"v2's replayed totals on the {name} are {got}, not the choice record's {want}"
            )
    no_verdict = sum(v is None for salt in V2_EXTRA_SALTS for v in replay.verdicts[salt].values())
    wrong = {
        salt: sorted(
            key
            for key in notes
            if replay.verdicts[salt][key] is not None
            and replay.verdicts[salt][key] != notes[key].supported
        )
        for salt in V2_EXTRA_SALTS
    }
    return {
        "scope": (
            "v2's three passes on the 125 tuning answers: its stored verdicts (salt judge, from "
            "the pass files' v2_passed) and its two extra passes of the choice step (salts "
            "judge-s2 and judge-s3), replayed from the call cache. Errors per pass and summed on "
            "both splits. The totals on v5's split are asserted equal to stage-a-choice.json's "
            "v2 entry."
        ),
        "judge_version": ONLINE_JUDGE_VERSION,
        "replayed_from_cache": {
            "salts": list(V2_EXTRA_SALTS),
            "calls": replay.usage.model_calls,
            "replies_without_a_verdict": no_verdict,
            "stored_salt_check": replay.stored_salt,
        },
        "errors_in_extra_passes": wrong,
        "readings": by_side,
        "matches_choice_record": True,
    }


def figure_c(notes: Mapping[str, Note], rev1: Rev1) -> dict[str, Any]:
    """Revision 1's errors per pass at every cell, the four answers, and the own-name check."""
    by_cell: dict[str, Any] = {}
    for t, s in CELLS:
        passes = rev1.all_passes(t, s)
        found: dict[str, Any] = {
            "v6_outside_named": reading(passes, notes, side(notes, "v6", False)),
            "v6_named": reading(passes, notes, side(notes, "v6", True)),
        }
        if (t, s) == CONTINUITY:
            found["v5_outside_named"] = reading(passes, notes, side(notes, "v5", False))
            found["v5_named"] = reading(passes, notes, side(notes, "v5", True))
        by_cell[cell(t, s)] = found
    t, s = CONTINUITY
    four = {}
    for key in FOUR:
        per_salt = []
        for p in rev1.passes:
            judged = p.assessments[key]
            low = min(judged.claims, key=lambda c: c.score) if judged.claims else None
            per_salt.append(
                {
                    "salt": p.salt,
                    "fully_supported": fully(judged, t, s),
                    "weakest_claim_score": None if low is None else low.score,
                    "weakest_claim": None if low is None else low.answer_span,
                    "weakest_claim_why": None if low is None else low.why,
                    "claims": [
                        {"answer_span": c.answer_span, "score": c.score, "quoted": c.quoted}
                        for c in judged.claims
                    ],
                }
            )
        four[key] = {"label_supported": notes[key].supported, "per_salt": per_salt}
    moved = []
    for salt, key, judged in rev1.judgings():
        before = fully(judged, t, s)
        after = fully(ignoring_own_name(judged, OWN_NAME), t, s)
        if before != after:
            moved.append({"key": key, "salt": salt, "from": before, "to": after})
    at_four = [m for m in moved if m["key"] == FOUR[2]]
    return {
        "scope": (
            "Revision 1's three passes. Per pass and summed: false passes and false fails on the "
            "93 and the 32 at every cell of the grid under test and at t = 70 with s off, where "
            "the 106 and the 19 are read too. The four answers behind v5's stop: their verdict at "
            "t = 70 with s off and their weakest claim under each salt. The own-name check: the "
            "verdicts at t = 70 with s off that move when a flag on the name Bedrock inside words "
            "of no claim is ignored, on docs-ai-003-p3 (the champion's run) and on any of the "
            "375 judgings."
        ),
        "errors_by_cell": by_cell,
        "four_answers": four,
        "own_name_ignored": {
            "name": OWN_NAME,
            "cell": cell(t, s),
            "judgings": sum(1 for _ in rev1.judgings()),
            "moved_on_docs_ai_003_p3": at_four,
            "moved_on_any_judging": moved,
        },
    }


def ignoring_own_name(found: Assessment, name: str) -> Assessment:
    """The assessment with every flag on `name` inside words of no claim taken out, so the verdict
    reads as if code had cleared the cited service's own name there (the proposal's change 2)."""
    kept_items = [
        item
        for item in found.flagged
        if not (item.kind == "not_claims" and normalize(item.token) == normalize(name))
    ]
    return found.model_copy(update={"flagged": kept_items})


def figure_d(notes: Mapping[str, Note], rev1: Rev1) -> dict[str, Any]:
    """Claim scores as levels, the weakest claims, and the whole-t sweep."""
    low, high = SCORE_BAND
    in_band = Counter(
        claim.score
        for _, _, found in rev1.judgings()
        for claim in found.claims
        if low <= claim.score <= high
    )
    band_total = sum(in_band.values())
    off_level = {score: n for score, n in sorted(in_band.items()) if score % 5}
    weakest_counts = Counter(w for _, _, a in rev1.judgings() if (w := weakest(a)) is not None)
    at_level = {
        f"{split}_outside_named": dict(
            Counter(
                "supported" if notes[key].supported else "unsupported"
                for _, key, a in rev1.judgings()
                if weakest(a) == 70 and not notes[key].named(split)
            )
        )
        for split in ("v5", "v6")
    }
    sweep = {}
    for split in ("v5", "v6"):
        keys = side(notes, split, False)
        rows = []
        for t in T_SWEEP:
            fp = ff = 0
            for salt in rev1.salts:
                a, b = errors(rev1.verdicts(salt, t, None), notes, keys)
                fp, ff = fp + a, ff + b
            rows.append({"t": t, "false_passes": fp, "false_fails": ff})
        sweep[f"{split}_outside_named"] = {"answers": len(keys), "s": "off", "rows": rows}
    return {
        "scope": (
            "Revision 1's 375 judgings (125 answers under three salts). Claim scores: every "
            "claim's score in the band 50 to 85, the share of them that are multiples of 5, and "
            "the off-level values. Weakest claims: judgings whose lowest claim score is 65, 70, "
            "72 or 75, and at 70 by label outside each named list. The sweep: totals over the "
            "three passes of false passes and false fails at every whole t from 50 to 90 with s "
            "off, on the 106 and on the 93."
        ),
        "claim_scores_in_band": {
            "band": list(SCORE_BAND),
            "claims": band_total,
            "multiples_of_5": sum(n for score, n in in_band.items() if score % 5 == 0),
            "off_level_values": off_level,
        },
        "weakest_claim": {
            "judgings_at": {str(level): weakest_counts.get(level, 0) for level in WEAKEST_LEVELS},
            "judgings_in_65_to_75": sum(n for w, n in weakest_counts.items() if 65 <= w <= 75),
            "at_70_by_label": at_level,
        },
        "t_sweep": sweep,
    }


def figure_e(notes: Mapping[str, Note], rev1: Rev1) -> dict[str, Any]:
    """The skeptic's search: its confidence where it mattered, and the s sweep."""
    t, s_off = CONTINUITY
    false_passes = []
    highest: dict[str, Any] = {}
    for salt, key, found in rev1.judgings():
        if not fully(found, t, s_off):
            continue
        search = found.skeptic.search if found.skeptic else None
        confidence = search.confidence if search and search.found else None
        if not notes[key].supported and not notes[key].named_v5:
            false_passes.append(
                {
                    "key": key,
                    "salt": salt,
                    "search_found": bool(search and search.found),
                    "confidence": confidence,
                    "search_span": search.answer_span if search and search.found else None,
                }
            )
        if notes[key].supported and confidence is not None:
            for scope, ok in (
                ("all_125", True),
                ("v5_outside_named", not notes[key].named_v5),
                ("v6_outside_named", not notes[key].named_v6),
            ):
                if ok and (scope not in highest or confidence > highest[scope]["confidence"]):
                    highest[scope] = {"confidence": confidence, "key": key, "salt": salt}
    sweep = {}
    for split in ("v5", "v6"):
        keys = side(notes, split, False)
        base_fp = base_ff = 0
        for salt in rev1.salts:
            a, b = errors(rev1.verdicts(salt, t, s_off), notes, keys)
            base_fp, base_ff = base_fp + a, base_ff + b
        rows = []
        for s in sorted(S_SWEEP):
            fp = ff = 0
            for salt in rev1.salts:
                a, b = errors(rev1.verdicts(salt, t, s), notes, keys)
                fp, ff = fp + a, ff + b
            rows.append(
                {
                    "s": s,
                    "false_passes": fp,
                    "false_fails": ff,
                    "false_passes_removed": base_fp - fp,
                    "false_fails_added": ff - base_ff,
                }
            )
        sweep[f"{split}_outside_named"] = {
            "answers": len(keys),
            "t": t,
            "s_off": {"false_passes": base_fp, "false_fails": base_ff},
            "rows": rows,
        }
    changed = {}
    for at in (70, 50):
        for s in V5_S_GRID[1:]:
            moved = sum(
                rev1.verdicts(salt, at, s)[key] != rev1.verdicts(salt, at, None)[key]
                for salt in rev1.salts
                for key in notes
            )
            changed[f"t={at} s={s}"] = moved
    return {
        "scope": (
            "Revision 1's 375 judgings. The search's confidence on every false pass at t = 70 "
            "with s off outside v5's named cases; the highest confidence on a judging of an "
            "answer labeled supported that passes at t = 70 with s off, over all 125 answers and "
            "outside each named list. The s sweep at t = 70 on the 106 and the 93: totals over "
            "the three passes, with the false passes removed and the false fails added against s "
            "off. Verdicts that s = 70 and s = 90 change against s off, over the 375 judgings, at "
            "t = 70 and at t = 50."
        ),
        "false_passes_at_t70_v5_outside_named": false_passes,
        "highest_confidence_on_supported_pass_at_t70": highest,
        "s_sweep": sweep,
        "verdicts_changed_by_s": changed,
    }


TARGET = re.compile(r"\]\(((?:[^()]|\([^()]*\))*)\)$")  # the (target) tail of one _LINK match


def link_kind(absolute: str) -> str:
    parts = urlparse(absolute)
    host, path = parts.netloc.lower(), parts.path.lower()
    if host.endswith("console.aws.amazon.com"):
        return "AWS console"
    if host == "docs.aws.amazon.com":
        return "documentation page"
    if "pricing" in path or host.startswith("pricing"):
        return "pricing page"
    return "other"


def figure_f(notes: Mapping[str, Note], outputs: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Links inside the 125 answers' quotes, and console links in the docs snapshot."""
    links = []
    for key in notes:
        raw = outputs[key].get("citations")
        citations = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        for number, citation in enumerate(citations, 1):
            quote, page = str(citation.get("quote", "")), str(citation.get("url", ""))
            seen = without_links(quote)
            for match in _LINK.finditer(quote):
                tail = TARGET.search(match.group(0))
                target = tail.group(1).strip().split(" ")[0] if tail else ""
                absolute = urljoin(page, target)
                text = match.group(1)
                links.append(
                    {
                        "key": key,
                        "quote": number,
                        "form": "image" if match.group(0).startswith("!") else "markdown link",
                        "text": text,
                        "target": absolute,
                        "kind": link_kind(absolute),
                        "text_kept_in_quote_shown": _occurs(text, normalize(seen)),
                    }
                )
            for match in _URL.finditer(_LINK.sub(r"\1", quote)):
                absolute = urljoin(page, match.group(0).strip("<>"))
                links.append(
                    {
                        "key": key,
                        "quote": number,
                        "form": "bare URL",
                        "text": None,
                        "target": absolute,
                        "kind": link_kind(absolute),
                        "text_kept_in_quote_shown": False,
                    }
                )
    by_kind = Counter(str(link["kind"]) for link in links)
    console, yes_cells, no_cells, quota_pages, pages, pages_with = 0, 0, 0, 0, 0, 0
    with SNAPSHOT_PAGES.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            page_record = json.loads(line)
            pages += 1
            found_console = False
            for match in _LINK.finditer(str(page_record["text"])):
                tail = TARGET.search(match.group(0))
                target = tail.group(1).strip().split(" ")[0] if tail else ""
                absolute = urljoin(str(page_record["url"]), target)
                if link_kind(absolute) != "AWS console":
                    continue
                console += 1
                found_console = True
                text = match.group(1).strip().casefold()
                yes_cells += text == YES_CELL
                no_cells += text == "no"
                quota_pages += urlparse(absolute).path.strip("/").startswith("servicequotas")
            pages_with += found_console
    return {
        "scope": (
            "The 125 tuning answers' cited quotes as stored (the text the claim judge takes the "
            "links out of with without_links), read from the results database: every markdown "
            "link or image found by the grader's _LINK pattern and every bare URL found by _URL "
            "after the links are removed, by the kind of its target, with whether the link's own "
            "text is still in the quote the judge is shown. Then every page of the docs snapshot: "
            "the links whose target is the AWS console, and how many of them are a cell reading "
            "Yes (the quota tables' adjustable column)."
        ),
        "in_quotes": {
            "links": len(links),
            "quotes_with_a_link": len({(link["key"], link["quote"]) for link in links}),
            "answers_with_a_link": len({link["key"] for link in links}),
            "by_kind": dict(sorted(by_kind.items())),
            "text_kept_in_quote_shown": sum(
                bool(link["text_kept_in_quote_shown"]) for link in links
            ),
            "links_found": links,
        },
        "in_snapshot": {
            "pages": pages,
            "pages_with_a_console_link": pages_with,
            "console_links": console,
            "yes_cells": yes_cells,
            "no_cells": no_cells,
            "targets_under_servicequotas": quota_pages,
        },
    }


def figure_g(
    pilot_raw: Mapping[str, Any],
    pilot: Mapping[str, Mapping[str, Assessment]],
    draft: PassRecord,
    rev1: Rev1,
    ledger: Mapping[str, Any],
    replay: V2Replay,
) -> dict[str, Any]:
    """Events over the distinct judgings, and the cost per answer of every pass."""
    judgings: dict[tuple[str, str, str], Assessment] = {}
    for salt, found in pilot.items():
        for key, a in found.items():
            judgings[(DRAFT, salt, key)] = a  # the pilot judged the first draft's version
    for key, a in draft.assessments.items():
        judgings[(DRAFT, draft.salt, key)] = a  # its salt judge repeats the pilot's on 20
    for salt, key, a in rev1.judgings():
        judgings[(REV1, salt, key)] = a
    held = list(judgings.values())
    problems = Counter(
        str(entry.problem)
        for a in held
        for claim in a.claims
        for entry in claim.support
        if entry.problem
    )
    cells = list(dict.fromkeys((*CELLS, *v5.CELLS)))  # the grid under test, then v5's
    flips = {cell(t, s): sum(a.dropped_span_flip(t, s) for a in held) for t, s in cells}
    pricing = load_config(v5.CLAIM_CONFIG).judge.model.pricing
    costs: dict[str, Any] = {
        "pilot": {
            "judgings": int(pilot_raw["events"]["judgings"]),
            "cost_per_answer_usd": round(float(pilot_raw["cost_per_answer_usd"]), 6),
            "output_token_share": None,
            "note": "the pilot's record holds its cost per answer, not its token usage",
        }
    }
    for record in (draft, *rev1.passes):
        cost = record.usage.cost_usd(pricing)
        if cost is None or pricing.output_per_mtok is None:
            raise Refused("configs/judge-v5.yaml has no judge pricing")
        output_cost = record.usage.output_tokens * pricing.output_per_mtok / 1_000_000
        costs[f"{record.label} {record.salt}"] = {
            "judgings": len(record.assessments),
            "calls": record.usage.model_calls,
            "calls_from_cache": record.usage.cached_calls,
            "cost_usd": round(cost, 6),
            "cost_per_answer_usd": round(cost / len(record.assessments), 6),
            "output_token_share": round(output_cost / cost, 4),
        }
    choose = next(e for e in ledger["entries"] if str(e["step"]).startswith("choose"))
    extra = [p for p in rev1.passes if p.salt in V2_EXTRA_SALTS]
    if any(p.usage.cached_calls for p in extra):
        raise Refused("revision 1's extra passes were not all sent to the API")
    claim_cost = sum(p.usage.cost_usd(pricing) or 0.0 for p in extra)
    claim_calls = sum(p.usage.model_calls for p in extra)
    v2_calls = int(choose["calls"]) - claim_calls
    v2_answers = len(draft.assessments) * len(V2_EXTRA_SALTS)
    if v2_calls != v2_answers or int(choose["uncached_calls"]) != int(choose["calls"]):
        raise Refused(
            "the choice step's ledger entry does not hold one uncached v2 call per answer"
        )
    v2_usd = float(choose["usd"]) - claim_cost
    return {
        "scope": (
            "Every distinct judging of the round: the pilot's 60 (20 answers under three salts), "
            "the first draft's 125 (its salt judge repeats the pilot's on those 20, the same "
            "cached replies) and revision 1's 375. Retries, judge errors, dropped support entries "
            "with the span check's reasons, searches naming words not in the answer, and the "
            "verdicts that fail only because of a dropped entry, at every cell of v5's grid and "
            "of the grid under test. Cost per answer and the output-token share per pass, from "
            "the pass files' usage at configs/judge-v5.yaml's pricing; v2's cost per answer from "
            "the ledger's choice entry less revision 1's two extra passes, and from its replayed "
            "replies' usage at configs/default.yaml's pricing."
        ),
        "judgings": {
            "pilot": len(pilot) * len(next(iter(pilot.values()))),
            "draft": len(draft.assessments),
            "rev1": sum(len(p.assessments) for p in rev1.passes),
            "distinct": len(judgings),
        },
        "events": {
            "retried": sum(a.retries > 0 for a in held),
            "judge_errors": sum(a.error is not None for a in held),
            "support_entries": sum(len(c.support) for a in held for c in a.claims),
            "dropped_support_entries": sum(a.dropped_support for a in held),
            "judgings_with_a_dropped_entry": sum(a.dropped_support > 0 for a in held),
            "dropped_for": dict(problems),
            "search_span_missing": sum(a.search_span_missing for a in held),
            "dropped_span_flips_by_cell": flips,
        },
        "cost": {
            "claim_judge_pricing": pricing.model_dump(),
            "per_pass": costs,
            "v2": {
                "ledger_choice_entry": {
                    "step": choose["step"],
                    "calls": choose["calls"],
                    "usd": choose["usd"],
                },
                "claim_judge_calls_in_it": claim_calls,
                "claim_judge_usd_in_it": round(claim_cost, 6),
                "v2_calls": v2_calls,
                "v2_usd": round(v2_usd, 6),
                "cost_per_answer_usd_from_ledger": round(v2_usd / v2_answers, 6),
                "cost_per_answer_usd_from_cached_replies": (
                    None
                    if replay.cost_per_answer_usd is None
                    else round(replay.cost_per_answer_usd, 6)
                ),
            },
        },
    }


def figure_h(notes: Mapping[str, Note], rev1: Rev1) -> dict[str, Any]:
    """Fail rates on the 125: v2's stored pass, and revision 1's passes per salt."""
    total = len(notes)
    v2_passes = sum(n.v2_passed for n in notes.values())
    by_cell = {}
    for t in (*T_GRID, CONTINUITY[0]):
        for s in (None, 49):
            per_salt = []
            for salt in rev1.salts:
                passes = sum(rev1.verdicts(salt, t, s).values())
                per_salt.append(
                    {"salt": salt, "passes": passes, "fail_rate": round(1 - passes / total, 4)}
                )
            by_cell[cell(t, s)] = per_salt
    return {
        "scope": (
            "The 125 tuning answers: the answers v2's stored verdict passes and its fail rate; "
            "revision 1's passes and fail rate under each salt at each grid t, and at t = 70, "
            "with s off and with s = 49."
        ),
        "answers": total,
        "v2_stored": {"passes": v2_passes, "fail_rate": round(1 - v2_passes / total, 4)},
        "rev1": by_cell,
    }


def figure_i(notes: Mapping[str, Note], rev1: Rev1, replay: V2Replay) -> dict[str, Any]:
    """The projected build check on the 32 named answers, labeled fitted."""
    t, s = CONTINUITY
    named = side(notes, "v6", True)
    moved = [k for k in named if not notes[k].named_v5]
    rev1_reading = reading(rev1.all_passes(t, s), notes, named)
    stored: dict[str, bool | None] = {key: note.v2_passed for key, note in notes.items()}
    v2_passes: list[tuple[str, Mapping[str, bool | None]]] = [(REV1_SALTS[0], stored)]
    v2_passes += [(salt, replay.verdicts[salt]) for salt in V2_EXTRA_SALTS]
    v2_reading = reading(v2_passes, notes, named)
    rev1_errors = int(rev1_reading["totals"]["errors"])
    fixes = sum(FITTED_FIXES.values())
    per_answer = {}
    for key in FITTED_FIXES:
        wrong = sum(rev1.verdicts(salt, t, s)[key] != notes[key].supported for salt in rev1.salts)
        per_answer[key] = {
            "rev1_errors_over_three_passes": wrong,
            "claimed_fixed": FITTED_FIXES[key],
        }
    return {
        "scope": (
            "The 32 named answers under v6's list, over three passes at t = 70 with s off. "
            "Revision 1's errors (judge errors would count, there are none), the errors the "
            "proposal's changes would remove on the answers they were written from, and the "
            "difference, labeled fitted: a projection on the passes the changes were fitted to, "
            "not evidence. Beside it, v2's errors on the same 32 over its stored pass and its two "
            "replayed passes."
        ),
        "label": "fitted",
        "rev1": {
            "errors_on_32": rev1_errors,
            "on_v5_named_19": int(
                reading(rev1.all_passes(t, s), notes, [k for k in named if notes[k].named_v5])[
                    "totals"
                ]["errors"]
            ),
            "on_moved_13": int(reading(rev1.all_passes(t, s), notes, moved)["totals"]["errors"]),
            "per_pass": rev1_reading["per_pass"],
        },
        "fixes_claimed": {"per_answer": per_answer, "total": fixes},
        "projected_errors_on_32_fitted": rev1_errors - fixes,
        "v2": {
            "errors_on_32": int(v2_reading["totals"]["errors"]),
            "per_pass": v2_reading["per_pass"],
        },
    }


# ---- the judge's reasons, classified ---------------------------------------------------------


@dataclass(frozen=True)
class ClassedClaim:
    """One classified claim of classes.json: its place in revision 1's passes (the answer, the
    salt and its index in the judging's claim table, from 0, as the file numbers it) and the first
    reader's class with the kind the reader gave an OTHER."""

    batch: int
    key: str
    salt: str
    index: int
    score: int
    answer_span: str
    cls: str
    other_kind: str | None

    @property
    def place(self) -> tuple[str, str, int]:
        return self.key, self.salt, self.index

    @property
    def two_type(self) -> bool:
        """A name or term in no quote, or a headerless table cell: the two types the rule fails."""
        return self.cls == TERM_IN_NO_QUOTE or (
            self.cls == OTHER and self.other_kind == HEADERLESS_CELL
        )

    def shown(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "claim": self.index,
            "batch": self.batch,
            "score": self.score,
            "class": self.cls,
            "answer_span": self.answer_span,
        }
        if self.other_kind is not None:
            found["other_kind"] = self.other_kind
        return found


Classed = dict[tuple[str, str], list[ClassedClaim]]  # (key, salt) -> its classified claims


def load_classes(rev1: Rev1) -> tuple[dict[str, Any], Classed]:
    """classes.json read as a record: every classified claim must sit in revision 1's passes as
    the file copies it (answer span, role, score, quoted flag and reason), no claim may be
    classified twice, and batch 1 must hold exactly the claims tied at the lowest score of every
    judging whose lowest score is in the band, since the categories-only rule reads them all."""
    raw = load_json(CLASSES)
    passes = {p.salt: p for p in rev1.passes}
    classed: Classed = {}
    places: set[tuple[str, str, int]] = set()
    for entry in raw["claims"]:
        key, salt, index = str(entry["key"]), str(entry["salt"]), int(entry["claim"])
        judged = passes.get(salt)
        found = judged.assessments.get(key) if judged is not None else None
        if found is None or not 0 <= index < len(found.claims):
            raise Refused(f"classes.json: claim {index} of {key} under {salt} is in no pass")
        claim = found.claims[index]
        copied = (claim.answer_span, claim.role, claim.score, claim.quoted, claim.why)
        given = tuple(entry[name] for name in ("answer_span", "role", "score", "quoted", "why"))
        if copied != given:
            raise Refused(f"classes.json: claim {index} of {key} under {salt} is not the pass's")
        reader = entry[READER]
        classed_claim = ClassedClaim(
            batch=int(entry["batch"]),
            key=key,
            salt=salt,
            index=index,
            score=claim.score,
            answer_span=claim.answer_span,
            cls=str(reader["class"]),
            other_kind=None if reader.get("other_kind") is None else str(reader["other_kind"]),
        )
        if classed_claim.place in places:
            raise Refused(f"classes.json: claim {index} of {key} under {salt} is classified twice")
        places.add(classed_claim.place)
        if classed_claim.cls not in raw["classes"]:
            raise Refused(f"classes.json: {classed_claim.cls!r} is not one of its classes")
        classed.setdefault((key, salt), []).append(classed_claim)
    low, high = SCORE_BAND
    tied_in_band = {
        (key, salt, index)
        for salt, key, found in rev1.judgings()
        if (lowest := weakest(found)) is not None and low <= lowest <= high
        for index, claim in enumerate(found.claims)
        if claim.score == lowest
    }
    batch_1 = {c.place for held in classed.values() for c in held if c.batch == BATCH_1}
    if batch_1 != tied_in_band:
        raise Refused(
            f"classes.json: batch 1 holds {len(batch_1)} claims, not the {len(tied_in_band)} tied "
            "at the lowest score of the judgings whose lowest score is in the band"
        )
    return raw, classed


def two_type_verdict(found: Assessment, classed: Sequence[ClassedClaim]) -> bool:
    """The two-type rule: the base verdict at the rule's cell, and no classified claim of the
    judging, in any batch, is a name or term in no quote or a headerless table cell."""
    return fully(found, *RULE_CELL) and not any(c.two_type for c in classed)


def categories_verdict(found: Assessment, classed: Sequence[ClassedClaim]) -> bool:
    """The categories-only rule: every claim tied at the judging's lowest score is stated outright
    or a plain rewording. A lowest score above the band passes (those claims were not classified;
    with no claims the lowest is read as 100, as Assessment.verdict reads it), one below fails."""
    low, high = SCORE_BAND
    lowest = weakest(found)
    if lowest is None or lowest > high:
        return True
    if lowest < low:
        return False
    by_index = {c.index: c for c in classed}
    return all(
        by_index[index].cls in PASSING_CLASSES
        for index, claim in enumerate(found.claims)
        if claim.score == lowest
    )


def deciding(rule: str, found: Assessment, classed: Sequence[ClassedClaim]) -> list[dict[str, Any]]:
    """The classified claims a rule's verdict turned on: the two types it fails, or the claims
    tied at the lowest score."""
    if rule == "two_type_rule":
        return [c.shown() for c in classed if c.two_type]
    lowest = weakest(found)
    return [c.shown() for c in classed if c.score == lowest and c.batch == BATCH_1]


def figure_j(
    notes: Mapping[str, Note], rev1: Rev1, raw: Mapping[str, Any], classed: Classed
) -> dict[str, Any]:
    """Two rules on the judge's classified reasons, simulated on revision 1's passes."""
    t, s = RULE_CELL
    scopes = {
        "v5_outside_named": side(notes, "v5", False),
        "v6_outside_named": side(notes, "v6", False),
        "v5_named": side(notes, "v5", True),
        "v6_named": side(notes, "v6", True),
    }
    texts = {
        "two_type_rule": (
            f"At {cell(t, s)}, a judging fails when revision 1 fails it (Assessment.verdict at "
            "that cell, the base verdict, flagged items included), or when any classified claim "
            f"of that judging, in any of the three batches, has the first reader's class "
            f"{TERM_IN_NO_QUOTE} (a name or term in no quote) or {OTHER} of the kind "
            f"'{HEADERLESS_CELL}'. Every other judging passes; a claim that was not classified "
            "is read as the base verdict reads it."
        ),
        "categories_only": (
            f"At {cell(t, s)}, the classes alone decide: a judging passes only when every claim "
            "tied at its lowest score has the first reader's class "
            f"{' or '.join(sorted(PASSING_CLASSES, reverse=True))} (stated outright, or a plain "
            "rewording, arithmetic or a unit conversion of what a quote states); any other class "
            f"at the lowest score fails it. A judging whose lowest score is above {SCORE_BAND[1]} "
            f"counts as passing and one whose lowest score is below {SCORE_BAND[0]} as failing, "
            "since those claims were not classified. The base verdict's other parts (flagged "
            "items, the search, dropped support entries) are not read."
        ),
    }
    rules: dict[str, Any] = {}
    for name, text in texts.items():
        passes: list[tuple[str, Mapping[str, bool | None]]] = []
        moved: list[dict[str, Any]] = []
        for p in rev1.passes:
            verdicts: dict[str, bool | None] = {}
            for key, found in p.assessments.items():
                held = classed.get((key, p.salt), [])
                if name == "two_type_rule":
                    verdict = two_type_verdict(found, held)
                else:
                    verdict = categories_verdict(found, held)
                verdicts[key] = verdict
                base = fully(found, t, s)
                if verdict != base:
                    moved.append(
                        {
                            "key": key,
                            "salt": p.salt,
                            "label_supported": notes[key].supported,
                            "named_v5": notes[key].named_v5,
                            "named_v6": notes[key].named_v6,
                            "rev1": base,
                            "rule": verdict,
                            "weakest_score": weakest(found),
                            "deciding_claims": deciding(name, found, held),
                        }
                    )
            passes.append((p.salt, verdicts))
        rules[name] = {
            "rule": text,
            "readings": {scope: reading(passes, notes, keys) for scope, keys in scopes.items()},
            "judgings_moved_against_rev1": moved,
        }
    rev1_totals = {}
    for scope, keys in scopes.items():
        fp = ff = 0
        for salt in rev1.salts:
            a, b = errors(rev1.verdicts(salt, t, s), notes, keys)
            fp, ff = fp + a, ff + b
        rev1_totals[scope] = {"answers": len(keys), "false_passes": fp, "false_fails": ff}
    later = [c for held in classed.values() for c in held if c.batch != BATCH_1]
    by_class = Counter(c.cls for c in later)
    by_batch = {
        str(batch): dict(sorted(Counter(c.cls for c in later if c.batch == batch).items()))
        for batch in sorted({c.batch for c in later})
    }
    terms = [c.shown() | {"key": c.key, "salt": c.salt} for c in later if c.cls == TERM_IN_NO_QUOTE]
    headerless = [
        c.shown() | {"key": c.key, "salt": c.salt}
        for c in later
        if c.cls == OTHER and c.other_kind == HEADERLESS_CELL
    ]
    recorded = raw["summary"]["batches_2_and_3"]
    counted = (len(later), len(terms), len(headerless))
    held_counts = (
        int(recorded["claims"]),
        int(recorded["term_in_no_quote"]),
        int(recorded["headerless_cell"]),
    )
    if counted != held_counts:
        raise Refused(
            f"classes.json: batches 2 and 3 count {counted} (claims, terms in no quote, "
            f"headerless cells), not the {held_counts} its summary records"
        )
    return {
        "scope": (
            f"Revision 1's {sum(1 for _ in rev1.judgings())} judgings at {cell(t, s)}, with the "
            "first reader's classes of the judge's reasons from classes.json (the second reader's "
            "classes on batch 1 are not read). Each rule is a verdict per judging, built from the "
            "saved scores, the base verdict and the saved classes, nothing judged anew. Per pass "
            "and summed: false passes and false fails on the 106 outside v5's named cases, the 93 "
            "outside v6's, the 19 v5 names and the 32 v6 names, beside revision 1's totals at the "
            "same cell, with every judging whose verdict a rule moves against revision 1's and "
            "the classified claims it turned on (claim: the index in the judging's claim table, "
            "from 0, as classes.json numbers it). Then the claims of batches 2 and 3 by class, "
            "checked against the file's own summary."
        ),
        "cell": cell(t, s),
        "classes_file": CLASSES.as_posix(),
        "classes_sha256": sha256_file(CLASSES),
        "reader": READER,
        "class_legend": dict(raw["classes"]),
        "classified_claims": sum(len(held) for held in classed.values()),
        "rev1_totals": rev1_totals,
        "rules": rules,
        "batches_2_and_3": {
            "claims": len(later),
            "by_class": {cls: by_class.get(cls, 0) for cls in raw["classes"]},
            "by_batch_and_class": by_batch,
            "term_in_no_quote": terms,
            "headerless_cell": headerless,
            "matches_classes_summary": True,
        },
    }


# ---- the run --------------------------------------------------------------------------------


def compute() -> tuple[dict[str, Any], V2Replay]:
    first = load_json(v5.pass_path(RESULTS, REV1, REV1_SALTS[0]))
    notes = load_notes(first)
    rev1 = Rev1([load_pass(REV1, salt, notes) for salt in REV1_SALTS])
    draft = load_pass(DRAFT, REV1_SALTS[0], notes)
    pilot_raw, pilot = load_pilot(draft)
    choice = load_json(RESULTS / v5.CHOICE_FILE)
    ledger = load_json(RESULTS / DEFAULT_PATH.name)
    config = load_config(v5.CLAIM_CONFIG)
    cases = {case.id: case for case in load_suite(v5.DEV_SUITE)}
    outputs = stored_outputs(config.paths.db, notes)  # read-only, closed before anything else
    replay = replay_v2(cases, outputs, notes)
    raw_classes, classed = load_classes(rev1)
    inputs = {
        str(path.as_posix()): sha256_file(path)
        for path in (
            RESULTS / PILOT_FILE,
            draft.path,
            *(p.path for p in rev1.passes),
            RESULTS / v5.CHOICE_FILE,
            RESULTS / DEFAULT_PATH.name,
            v5.CLAIM_CONFIG,
            v5.V2_CONFIG,
        )
    }
    figures: dict[str, Any] = {
        "about": (
            "The figures docs/plans/judge-v6.md cites, recomputed from the records of the claim "
            "judge's first round by scripts/judge_v6_figures.py. Nothing was judged anew: every "
            "figure re-reads the round's saved replies with the claim judge's own code, and v2's "
            "two extra passes are replayed from the call cache with no call to the API."
        ),
        "inputs_sha256": inputs,
        "grid_under_test": {
            "t": list(T_GRID),
            "s": ["off" if s is None else s for s in S_GRID],
            "continuity_cell": cell(*CONTINUITY),
        },
        "a_tuning_set_split": figure_a(notes, rev1),
        "b_v2_three_passes": figure_b(notes, replay, choice),
        "c_rev1_errors": figure_c(notes, rev1),
        "d_scores": figure_d(notes, rev1),
        "e_skeptic": figure_e(notes, rev1),
        "f_links": figure_f(notes, outputs),
        "g_events_and_cost": figure_g(pilot_raw, pilot, draft, rev1, ledger, replay),
        "h_fail_rates": figure_h(notes, rev1),
        "i_build_check_fitted": figure_i(notes, rev1, replay),
        "j_reason_rules": figure_j(notes, rev1, raw_classes, classed),
    }
    return figures, replay


def write_outputs(figures: dict[str, Any], replay: V2Replay) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES.write_text(json.dumps(figures, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    held = {
        "about": (
            "v2's two extra passes of the claim judge round's choice step (docs/plans/judge-v5.md, "
            "stage A step 5), replayed from the call cache by scripts/judge_v6_figures.py: the "
            "request JudgeGrader makes from configs/default.yaml with the responsive question off, "
            "under the salts below, answered by the cached reply alone. No call was sent. "
            "judge_supported is the verdict the choice step counted; judge_core_supported is "
            "recorded where the reply held one."
        ),
        "judge_version": ONLINE_JUDGE_VERSION,
        "salts": list(V2_EXTRA_SALTS),
        "answers": len(replay.records) // len(V2_EXTRA_SALTS),
        "verdicts": replay.records,
    }
    V2_PASSES.write_text(json.dumps(held, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _errors(found: Mapping[str, Any]) -> str:
    """A reading's errors per pass and summed, as FP/FF."""
    per = " ".join(f"{p['false_passes']}/{p['false_fails']}" for p in found["per_pass"])
    totals = found["totals"]
    return f"{per} = {totals['false_passes']}/{totals['false_fails']}"


def _print(figures: Mapping[str, Any]) -> None:
    a, b, c = figures["a_tuning_set_split"], figures["b_v2_three_passes"], figures["c_rev1_errors"]
    d, e, f = figures["d_scores"], figures["e_skeptic"], figures["f_links"]
    g, h = figures["g_events_and_cost"], figures["h_fail_rates"]
    i, j = figures["i_build_check_fitted"], figures["j_reason_rules"]
    lines = ["judge v6 figures, from the saved records (no call was made)", ""]
    lines.append("a. the tuning set under each named list (v2's stored errors as FP/FF)")
    for split, sides in a["splits"].items():
        out, named, kept_set = sides["outside_named"], sides["named"], sides["kept"]
        lines.append(
            f"   {split}: outside {out['answers']} ({out['labeled_supported']} supported; v2 "
            f"{out['v2_stored_false_passes']}/{out['v2_stored_false_fails']}); named "
            f"{named['answers']} ({named['labeled_supported']} supported; v2 "
            f"{named['v2_stored_false_passes']}/{named['v2_stored_false_fails']}); kept "
            f"{kept_set['answers']}"
        )
    lines.append(f"   answers moved: {a['answers_moved']['count']}")
    for key, cells in a["splits"]["v6"]["kept"]["rev1_passes_of_three"].items():
        passes = ", ".join(f"{name}: {n}" for name, n in cells.items())
        lines.append(f"   kept {key}: revision 1 passes of 3 at {passes}")
    lines.append("b. v2's three passes (stored, judge-s2, judge-s3 from the cache), FP/FF")
    for name, found in b["readings"].items():
        lines.append(f"   {name} ({found['answers']}): {_errors(found)}")
    lines.append(f"   stored salt check: {b['replayed_from_cache']['stored_salt_check']}")
    lines.append("c. revision 1's errors per pass, FP/FF")
    for name, found in c["errors_by_cell"].items():
        parts = [f"{scope} ({r['answers']}): {_errors(r)}" for scope, r in found.items()]
        lines.append(f"   {name}: " + "; ".join(parts))
    for key, found in c["four_answers"].items():
        per = ", ".join(
            f"{p['salt']} {'pass' if p['fully_supported'] else 'fail'}, weakest claim "
            f"{p['weakest_claim_score']}"
            for p in found["per_salt"]
        )
        lines.append(f"   {key} (supported={found['label_supported']}): {per}")
    own = c["own_name_ignored"]
    where = ", ".join(f"{m['key']} {m['salt']}" for m in own["moved_on_any_judging"])
    lines.append(
        f"   {own['name']} ignored at {own['cell']}: {len(own['moved_on_docs_ai_003_p3'])} "
        f"verdicts move on docs-ai-003-p3, {len(own['moved_on_any_judging'])} on any of "
        f"{own['judgings']} judgings: {where}"
    )
    band = d["claim_scores_in_band"]
    lines.append(
        f"d. claim scores in {band['band']}: {band['multiples_of_5']} of {band['claims']} "
        f"multiples of 5; off-level {band['off_level_values']}"
    )
    weak = d["weakest_claim"]
    lines.append(
        f"   weakest claim at {weak['judgings_at']}; in 65..75: {weak['judgings_in_65_to_75']}; "
        f"at 70 by label: {weak['at_70_by_label']}"
    )
    for name, sweep in d["t_sweep"].items():
        row = " ".join(f"{r['t']}:{r['false_passes']}/{r['false_fails']}" for r in sweep["rows"])
        lines.append(f"   t sweep {name} ({sweep['answers']}), FP/FF totals: {row}")
    lines.append("e. the skeptic")
    for fp in e["false_passes_at_t70_v5_outside_named"]:
        lines.append(f"   false pass {fp['key']} {fp['salt']}: confidence {fp['confidence']}")
    highest = e["highest_confidence_on_supported_pass_at_t70"]
    lines.append(f"   highest confidence on a supported pass at t=70: {highest}")
    for name, sweep in e["s_sweep"].items():
        row = " ".join(
            f"{r['s']}:-{r['false_passes_removed']}/+{r['false_fails_added']}"
            for r in sweep["rows"]
        )
        lines.append(f"   s sweep {name} ({sweep['answers']}), FP removed/FF added: {row}")
    lines.append(f"   verdicts changed by s: {e['verdicts_changed_by_s']}")
    q, sn = f["in_quotes"], f["in_snapshot"]
    lines.append(
        f"f. links in quotes: {q['links']} in {q['quotes_with_a_link']} quotes over "
        f"{q['answers_with_a_link']} answers, by kind {q['by_kind']}; snapshot: "
        f"{sn['console_links']} console links, {sn['yes_cells']} Yes cells"
    )
    lines.append(f"g. judgings {g['judgings']}; events {g['events']}")
    for name, cost in g["cost"]["per_pass"].items():
        lines.append(
            f"   {name}: ${cost['cost_per_answer_usd']} per answer, output share "
            f"{cost['output_token_share']}"
        )
    lines.append(f"   v2: {g['cost']['v2']}")
    lines.append(f"h. fail rates: v2 stored passes {h['v2_stored']}")
    for name, per_salt in h["rev1"].items():
        rates = ", ".join(f"{p['salt']} {p['passes']} ({p['fail_rate']})" for p in per_salt)
        lines.append(f"   {name}: {rates}")
    lines.append(
        f"i. build check (fitted): revision 1 {i['rev1']['errors_on_32']} on the 32 (on the 19: "
        f"{i['rev1']['on_v5_named_19']}, on the moved 13: {i['rev1']['on_moved_13']}) minus "
        f"{i['fixes_claimed']['total']} = {i['projected_errors_on_32_fitted']}; v2 "
        f"{i['v2']['errors_on_32']}"
    )
    base = "; ".join(
        f"{scope} ({r['answers']}): {r['false_passes']}/{r['false_fails']}"
        for scope, r in j["rev1_totals"].items()
    )
    lines.append(f"j. rules on the judge's classified reasons at {j['cell']}, FP/FF")
    lines.append(f"   revision 1 totals: {base}")
    for name, rule in j["rules"].items():
        parts = [f"{scope} ({r['answers']}): {_errors(r)}" for scope, r in rule["readings"].items()]
        lines.append(f"   {name}: " + "; ".join(parts))
        lines.append(
            f"   {name} moves {len(rule['judgings_moved_against_rev1'])} verdicts against "
            "revision 1"
        )
    later = j["batches_2_and_3"]
    terms = ", ".join(
        f"{c['key']} {c['salt']} claim {c['claim']}" for c in later["term_in_no_quote"]
    )
    lines.append(
        f"   batches 2 and 3 ({later['claims']} claims) by class: {later['by_class']}; term in no "
        f"quote: {terms or 'none'}; headerless cells: {len(later['headerless_cell'])}"
    )
    print("\n".join(lines))


def main() -> int:
    try:
        figures, replay = compute()
    except (Refused, CacheMiss) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    write_outputs(figures, replay)
    _print(figures)
    print(f"\nwritten: {FIGURES.as_posix()} and {V2_PASSES.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
