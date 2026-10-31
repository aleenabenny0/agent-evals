"""Stage A of judge v6's validation (docs/plans/judge-v6.md, approved 2026-10-30).

    uv run python scripts/judge_compare_v6.py replay-check
    uv run python scripts/judge_compare_v6.py stage-a LABEL [--gap-types]
    uv run python scripts/judge_compare_v6.py choose LABEL_A:T_A:S_A [LABEL_B:T_B:S_B]

replay-check  Before any paid pass, once per code version: the claim judge's verdict code as it
              is now, run over judge v5's saved passes (revision 1's three) with no call to the
              API, must reproduce the figures the design pre-registers ("The versions, as
              planned"): the errors on the 93 at every cell, the errors on the 125 at every cell,
              the kept table, and docs-ai-003-p3's two verdicts moved to pass. Its record gates
              the first paid pass.
stage-a       Steps 1 to 4. One version of the claim judge (the first draft, or a revision), named
              by LABEL, judged on all 125 tuning answers under three fresh salts
              (judge-v6-LABEL-1 to -3). The first version's first 20 answers (v5's draw rule,
              seed judge-v6-pilot) are the pilot: its cost per answer is measured and the work
              stops if it is more than 1.3 times $0.012; those 60 judgings count among the
              passes. Prints the grid of t and s on the three-pass means against v2's three
              passes, the eligible cells (the total gate on the 125, the caps on the 93, the kept
              rule, the judge-error cap), the candidate order over every version judged so far,
              the build check as a prediction, and every reported figure. A version is judged
              once; asked again, it is read back; one label per version. --gap-types asks the
              judge for a gap type per claim (revision 1); without it the prompts are v5's.
choose        Steps 5 and 6. The two candidates of that order, each a stage-a label and a cell,
              judged under three more fresh salts (judge-v6-choice-1 to -3), refused if any
              claim-judge call under them is served by the cache; the frozen version is the first
              in step 4's order that is eligible on those means, or the round stops.

Every step that judges costs money and is judge v6's round's spend (results/judge-v6/spend.json,
cap $60): checked against the ledger before the first call, stopped during the step when the cap
is reached, and recorded after, however the step ends. v2's three passes are its stored verdicts
and the two passes replayed from the call cache for the v6 proposal
(results/judge-v5/analysis/v2-extra-passes.json), whose totals must equal v5's choice record. The
results database is opened read-only and closed before any call, and no grade is stored: what the
script finds goes to results/judge-v6/.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any

import judge_compare_v5 as v5
import typer
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rich.progress import BarColumn, Progress, TextColumn

from agent_evals import spend
from agent_evals.config import Config, JudgeConfig, load_config
from agent_evals.graders import claim_judge as cj
from agent_evals.graders.claim_judge import (
    GRADING_CODE_VERSION,
    RULES_VERSION,
    S_GRID,
    T_GRID,
    Assessment,
    ClaimJudgeGrader,
    JudgingCounts,
)
from agent_evals.graders.service_names import ServiceNames
from agent_evals.harness.cache import CallCache, sha256_text
from agent_evals.harness.llm import AnthropicClient, LLMClient, LLMResponse
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.spend import MeteredLLM, PilotRefusal, SpendCapError, SpendEntry, SpendLedger
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import Usage

# The tuning set is v5's (scripts/judge_compare_v5.py): the 125 judge-dev labels on the three dev
# runs, joined to v2's stored verdicts. The round's files sit beside its ledger.
ROUND = "judge-v6"
OUT_DIR = spend.V6_PATH.parent  # results/judge-v6
CLAIM_CONFIG = Path("configs/judge-v6.yaml")
V2_CONFIG = v5.V2_CONFIG
V2_EXTRA_PASSES = Path("results/judge-v5/analysis/v2-extra-passes.json")
V5_CHOICE = v5.OUT_DIR / v5.CHOICE_FILE  # results/judge-v5/stage-a-choice.json
V5_REV1 = "rev1"  # judge v5's frozen version, whose three passes the replay check reads
# The named cases: v5's 25 plus the five the design was written from (appendix, frozen by
# decision 3). Every run's answer on a named case leaves the capped count.
ADDED_CASES = frozenset(
    {"docs-ai-003-p1", "docs-ai-003-p3", "docs-chg-010", "docs-mix-042", "docs-mix-058"}
)
NAMED_CASES = v5.NAMED_CASES | ADDED_CASES
KNOWN_MISS = "docs-mix-058"  # pre-registered (decision 8): exempt from the kept rule, reported
# What the design counts on the tuning set. Any other set is refused, since the rules name these.
TUNING_ANSWERS = 125
NAMED_ANSWERS = 32
CAPPED_ANSWERS = 93
KEPT_ANSWERS = 7  # named, labeled supported, passed by v2's stored verdict; one is the known miss
# Salts, named in advance and never reused from v5 (a reused name replays the cached reply).
PASSES = 3
CHOICE = "choice"
V2_STORED_SALT = "judge"  # v2's stored verdicts, its first pass
V2_REPLAYED_SALTS = ("judge-s2", "judge-s3")  # its two extra passes, replayed from the cache
# The grid (step 2), the claim judge's own, in its order. s off before 59 before 49 (step 4).
CELLS: tuple[tuple[int, int | None], ...] = tuple((t, s) for t in T_GRID for s in S_GRID)
S_ORDER = {None: 0, 59: 1, 49: 2}
MAX_VERSIONS = 4  # the first draft and at most three revisions
JUDGE_ERROR_CAP = Fraction(2, 100)  # of the 125, in every pass
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# The pilot (step 1): v5's draw rule under a new seed; 20 answers, 7, 7 and 6 from the dev runs.
PILOT_SEED = "judge-v6-pilot"
PILOT_PER_RUN = v5.PILOT_PER_RUN
PILOT_FILE = spend.PILOT_FILE
REPLAY_FILE = "replay-check.json"
CHOICE_FILE = "stage-a-choice.json"

# The figures the replay check must reproduce (docs/plans/judge-v6.md, "The versions, as
# planned"): over revision 1's three saved passes, read by the code in place with the cited
# services' names filled in. Errors (false passes, false fails) on the 93 at each level, the
# errors on the 125 at each cell (revision 1's less the two verdicts the own-name clearing moves),
# and the kept table.
REPLAY_93: dict[int, tuple[int, int]] = {64: (2, 0), 69: (0, 0), 74: (0, 3)}
REPLAY_125: dict[tuple[int, int | None], int] = {
    (64, None): 26,
    (64, 49): 21,
    (64, 59): 25,
    (69, None): 23,
    (69, 49): 18,
    (69, 59): 22,
    (74, None): 18,
    (74, 49): 18,
    (74, 59): 18,
}
REPLAY_V2_125 = 24  # v2's errors on the 125 over its three passes (9, 8 and 7)
REPLAY_MOVED = f"{v5.RUNS[1]}/docs-ai-003-p3"  # passes 3 of 3 at every cell once read by v6
REPLAY_KEPT_75_FAILS = f"{v5.RUNS[2]}/docs-chg-010"  # 3 of 3 at the 65 and 70 levels, 0 of 3 at 75

Verdicts = dict[str, bool | None]  # answer key -> fully supported; None: a judge error


class Refused(Exception):
    """The script stops: the data, a file or a request is not what the design names."""


# ---- the tuning set ---------------------------------------------------------------------------


def named(answer: v5.Answer) -> bool:
    return answer.case.id in NAMED_CASES


def kept(answers: Iterable[v5.Answer]) -> list[v5.Answer]:
    """The named answers labeled supported that v2's stored verdict passes, the known miss among
    them (it is reported, and exempt from the rule: kept_rule)."""
    return [a for a in answers if named(a) and a.supported and a.v2_passed]


def kept_rule(answers: Iterable[v5.Answer]) -> list[v5.Answer]:
    return [a for a in kept(answers) if a.case.id != KNOWN_MISS]


def check_tuning_set(answers: Sequence[v5.Answer]) -> None:
    shape = (len(answers), sum(named(a) for a in answers), len(kept(answers)))
    if shape != (TUNING_ANSWERS, NAMED_ANSWERS, KEPT_ANSWERS):
        raise Refused(
            f"the tuning set has {shape[0]} answers, {shape[1]} on named cases and {shape[2]} of "
            f"those kept; the design counts {TUNING_ANSWERS}, {NAMED_ANSWERS} and {KEPT_ANSWERS}"
        )
    if not any(a.case.id == KNOWN_MISS for a in kept(answers)):
        raise Refused(f"the known miss {KNOWN_MISS} is not among the kept answers")


def stored_v2(answers: Iterable[v5.Answer]) -> Verdicts:
    return {a.key: a.v2_passed for a in answers}


def v2_passes(answers: Sequence[v5.Answer]) -> list[tuple[str, Verdicts]]:
    """v2's three passes: its stored verdicts and the two passes replayed from the call cache for
    the proposal, read from their committed record, whose totals must equal v5's choice record
    (1 false pass and 1 false fail on the 106; 12 and 10 on the 19)."""
    if not V2_EXTRA_PASSES.is_file():
        raise Refused(f"{V2_EXTRA_PASSES} is missing: v2's two replayed passes are read from it")
    raw = json.loads(V2_EXTRA_PASSES.read_text(encoding="utf-8"))
    if raw.get("judge_version") != ONLINE_JUDGE_VERSION or tuple(raw.get("salts", ())) != (
        V2_REPLAYED_SALTS
    ):
        raise Refused(f"{V2_EXTRA_PASSES} does not hold v2's passes under {V2_REPLAYED_SALTS}")
    by_salt: dict[str, Verdicts] = {salt: {} for salt in V2_REPLAYED_SALTS}
    for entry in raw["verdicts"]:
        by_salt[str(entry["salt"])][str(entry["key"])] = entry["judge_supported"]
    keys = {a.key for a in answers}
    for salt, verdicts in by_salt.items():
        if set(verdicts) != keys:
            raise Refused(f"{V2_EXTRA_PASSES}: the pass under {salt} is not on the tuning set")
    passes = [(V2_STORED_SALT, stored_v2(answers))]
    passes += [(salt, by_salt[salt]) for salt in V2_REPLAYED_SALTS]
    choice = json.loads(V5_CHOICE.read_text(encoding="utf-8"))["v2"]
    for side, wanted in (("others", lambda a: not a.named), ("named", lambda a: a.named)):
        keys_side = [a.key for a in answers if wanted(a)]
        got = count(keys_side, [p for _, p in passes], {a.key: a for a in answers})
        want = choice[side]["totals"]
        if (got.false_passes, got.false_fails) != (want["false_passes"], want["false_fails"]):
            raise Refused(
                f"v2's replayed totals on v5's {side} are {got.false_passes} false passes and "
                f"{got.false_fails} false fails, not the choice record's {want}"
            )
    return passes


# ---- counting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """A judge's errors against the labels on some answers, summed over its passes. A judge
    error is counted on its own; `errors` leaves it out and `errors_and_judge_errors` counts each
    as one (the named cases and the total on the 125: an answer a judge cannot judge is not one
    it gets right)."""

    answers: int
    passes: int
    false_passes: int
    false_fails: int
    judge_errors: int

    @property
    def errors(self) -> int:
        return self.false_passes + self.false_fails

    @property
    def errors_and_judge_errors(self) -> int:
        return self.errors + self.judge_errors

    def mean(self, total: int) -> Fraction:
        return Fraction(total, self.passes) if self.passes else Fraction(0)


def count(
    keys: Sequence[str], passes: Sequence[Verdicts], answers: Mapping[str, v5.Answer]
) -> Counts:
    false_passes = false_fails = judge_errors = 0
    for verdicts in passes:
        for key in keys:
            verdict = verdicts[key]
            if verdict is None:
                judge_errors += 1
            elif verdict and not answers[key].supported:
                false_passes += 1
            elif not verdict and answers[key].supported:
                false_fails += 1
    return Counts(len(keys), len(passes), false_passes, false_fails, judge_errors)


@dataclass(frozen=True)
class Reading:
    """One judge (a version at one cell, or v2) over its passes: the capped count (the 93), the
    named answers (the 32), the 125, v5's split for continuity, the kept rule and the known miss."""

    capped: Counts  # the 93
    named: Counts  # the 32
    total: Counts  # the 125
    v5_others: Counts  # the 106
    v5_named: Counts  # the 19
    kept_passes: dict[str, int]  # each kept answer -> passes it is passed in
    passes: int

    @property
    def total_errors(self) -> Fraction:
        """The total gate's figure: errors on the 93 (judge errors aside) plus errors and judge
        errors on the 32, per pass."""
        return self.capped.mean(self.capped.errors) + self.named.mean(
            self.named.errors_and_judge_errors
        )

    def keeps(self, answers: Sequence[v5.Answer]) -> bool:
        """Every kept answer but the known miss is passed on at least two of three passes (or on
        the one pass of a single-pass reading)."""
        need = self.passes // 2 + 1
        return all(self.kept_passes[a.key] >= need for a in kept_rule(answers))

    def judge_errors_per_pass(self) -> Fraction:
        return self.total.mean(self.total.judge_errors)


def read(answers: Sequence[v5.Answer], passes: Sequence[Verdicts]) -> Reading:
    by_key = {a.key: a for a in answers}
    return Reading(
        capped=count([a.key for a in answers if not named(a)], passes, by_key),
        named=count([a.key for a in answers if named(a)], passes, by_key),
        total=count([a.key for a in answers], passes, by_key),
        v5_others=count([a.key for a in answers if not a.named], passes, by_key),
        v5_named=count([a.key for a in answers if a.named], passes, by_key),
        kept_passes={a.key: sum(p[a.key] is True for p in passes) for a in kept(answers)},
        passes=len(passes),
    )


@dataclass(frozen=True)
class Gates:
    """Step 3's conditions on a reading against v2's."""

    total_gate: bool  # fewer errors than v2 on the 125
    caps: bool  # on the 93: errors at most v2's plus 2, false fails at most v2's plus 1
    keeps: bool
    judge_error_cap: bool  # at most 2% of the 125 in every pass

    @property
    def eligible(self) -> bool:
        return self.total_gate and self.caps and self.keeps and self.judge_error_cap


def gates(
    judge: Reading, v2: Reading, answers: Sequence[v5.Answer], judge_errors_by_pass: Sequence[int]
) -> Gates:
    mine, theirs = judge.capped, v2.capped
    return Gates(
        total_gate=judge.total_errors < v2.total_errors,
        caps=(
            mine.mean(mine.errors) <= theirs.mean(theirs.errors) + 2
            and mine.mean(mine.false_fails) <= theirs.mean(theirs.false_fails) + 1
        ),
        keeps=judge.keeps(answers),
        judge_error_cap=all(
            Fraction(n, judge.total.answers) <= JUDGE_ERROR_CAP for n in judge_errors_by_pass
        ),
    )


def build_check(judge: Reading, v2: Reading) -> bool:
    """The prediction, reported and not gated: fewer errors than v2 on the 32, judge errors
    counted as errors for both."""
    mine, theirs = judge.named, v2.named
    return mine.mean(mine.errors_and_judge_errors) < theirs.mean(theirs.errors_and_judge_errors)


def step4_key(
    reading: Reading, t: int, s: int | None, version_order: int
) -> tuple[Fraction, Fraction, int, int, int]:
    """Step 4's order: fewest errors on the 125 (judge errors counted on the named cases), then
    fewest on the 93, then lower t, then s off before 59 before 49, then the version judged
    first."""
    return (
        reading.total_errors,
        reading.capped.mean(reading.capped.errors),
        t,
        S_ORDER[s],
        version_order,
    )


# ---- a version's passes -----------------------------------------------------------------------


def _s(s: int | None) -> str:
    return "off" if s is None else str(s)


def cell_name(t: int, s: int | None) -> str:
    return f"t={t} s={_s(s)}"


CELL_NAMES = frozenset(cell_name(t, s) for t, s in CELLS)


class CellVerdict(BaseModel):
    fully_supported: bool
    core_supported: bool
    dropped_span_flip: bool
    rule_fails: int
    names_cleared: int
    names_cleared_skeptic_low: int


class AnswerVerdicts(BaseModel):
    """One answer under one salt, as the claim judge's code read it when it was judged: its
    verdicts at each of the nine cells, or a judge error, which has a verdict at none."""

    judge_error: bool
    cells: dict[str, CellVerdict] = Field(default_factory=dict)


def read_verdicts(found: Assessment) -> AnswerVerdicts:
    if found.error is not None:
        return AnswerVerdicts(judge_error=True)
    cells: dict[str, CellVerdict] = {}
    for t, s in CELLS:
        v = found.verdict(t, s)
        cells[cell_name(t, s)] = CellVerdict(
            fully_supported=v.fully_supported,
            core_supported=v.core_supported,
            dropped_span_flip=found.dropped_span_flip(t, s),
            rule_fails=v.rule_fails,
            names_cleared=v.names_cleared,
            names_cleared_skeptic_low=v.names_cleared_skeptic_low,
        )
    return AnswerVerdicts(judge_error=False, cells=cells)


def claim_verdicts(verdicts: Mapping[str, AnswerVerdicts], t: int, s: int | None) -> Verdicts:
    name = cell_name(t, s)
    return {
        key: None if found.judge_error else found.cells[name].fully_supported
        for key, found in verdicts.items()
    }


def dropped_span_flips(verdicts: Mapping[str, AnswerVerdicts], t: int, s: int | None) -> int:
    name = cell_name(t, s)
    return sum(
        not found.judge_error and found.cells[name].dropped_span_flip for found in verdicts.values()
    )


def at_cell(config: JudgeConfig, t: int, s: int | None) -> JudgeConfig:
    return config.model_copy(update={"support_threshold": t, "skeptic_threshold": s})


def versions(llm: LLMClient, config: JudgeConfig, names: ServiceNames) -> dict[str, str]:
    """The claim judge's version at each cell: t and s are part of a version, so each cell is a
    judge of its own; equal maps mean the same prompts, code, word lists and service names."""
    return {
        cell_name(t, s): ClaimJudgeGrader(llm, at_cell(config, t, s), service_names=names).version
        for t, s in CELLS
    }


def _nothing() -> None:
    return None


@dataclass(frozen=True)
class Pass:
    salt: str
    assessments: dict[str, Assessment]
    verdicts: dict[str, AnswerVerdicts]
    usage: Usage
    counts: JudgingCounts  # the grader's own counts at the config's t and s


def judge_pass(
    llm: LLMClient,
    config: JudgeConfig,
    names: ServiceNames,
    answers: Sequence[v5.Answer],
    salt: str,
    *,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> Pass:
    grader = ClaimJudgeGrader(llm, config, salt=salt, service_names=names)

    def one(answer: v5.Answer) -> tuple[str, Assessment]:
        found = grader.assess(answer.case, answer.result)
        if found is None:
            raise Refused(f"{answer.key}: the claim judge finds nothing to judge in it")
        return answer.key, found

    assessments: dict[str, Assessment] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, found in pool.map(one, answers):
            assessments[key] = found
            tick()
    verdicts = {key: read_verdicts(found) for key, found in assessments.items()}
    return Pass(salt, assessments, verdicts, grader.usage, grader.counts)


class PassFile(BaseModel):
    """One pass of one version over the tuning answers, written once: the verdicts its code read
    at every cell when it was judged, the grader's counts, and the assessments as judged."""

    label: str
    salt: str
    gap_types: bool
    judged_at: str
    versions: dict[str, str]
    answers: list[v5.AnswerNote]
    verdicts: dict[str, AnswerVerdicts]
    counts: dict[str, int]
    assessments: dict[str, dict[str, Any]]
    usage: Usage


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def notes(answers: Iterable[v5.Answer]) -> list[v5.AnswerNote]:
    return [
        v5.AnswerNote(key=a.key, named=named(a), supported=a.supported, v2_passed=a.v2_passed)
        for a in answers
    ]


def pass_salt(label: str, n: int) -> str:
    return f"{ROUND}-{label}-{n}"


def pass_path(out_dir: Path, label: str, n: int) -> Path:
    return out_dir / f"stage-a-{label}.pass{n}.json"


def save_pass(
    out_dir: Path,
    label: str,
    n: int,
    judged: Pass,
    judge_versions: Mapping[str, str],
    gap_types: bool,
    answers: Sequence[v5.Answer],
) -> None:
    held = PassFile(
        label=label,
        salt=judged.salt,
        gap_types=gap_types,
        judged_at=_now(),
        versions=dict(judge_versions),
        answers=notes(answers),
        verdicts=judged.verdicts,
        counts=judged.counts.model_dump(),
        assessments={key: a.model_dump(mode="json") for key, a in judged.assessments.items()},
        usage=judged.usage,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with pass_path(out_dir, label, n).open("x", encoding="utf-8") as fh:  # once
        fh.write(held.model_dump_json(indent=1) + "\n")


def load_pass(
    out_dir: Path, label: str, n: int, answers: Sequence[v5.Answer], current: Mapping[str, str]
) -> PassFile:
    """A saved pass, checked against the tuning set. When it is the version of the code in place,
    that code reads its assessments again and must find the verdicts saved."""
    path = pass_path(out_dir, label, n)
    held = PassFile.model_validate_json(path.read_text(encoding="utf-8"))
    if (held.label, held.salt) != (label, pass_salt(label, n)):
        raise Refused(f"{path} holds {held.label!r} under salt {held.salt!r}")
    keys = {a.key for a in answers}
    if (
        held.answers != notes(answers)
        or set(held.verdicts) != keys
        or set(held.assessments) != keys
    ):
        raise Refused(f"{path} was judged on another tuning set than the store holds now")
    if any(
        set(v.cells) != (set() if v.judge_error else CELL_NAMES) for v in held.verdicts.values()
    ):
        raise Refused(f"{path} does not hold each answer's verdicts at the nine cells")
    if held.versions == current:
        now = {key: Assessment.model_validate(raw) for key, raw in held.assessments.items()}
        changed = sorted(k for k, a in now.items() if read_verdicts(a) != held.verdicts[k])
        if changed:
            raise Refused(
                f"{path}: the code in place has the version this pass was judged with, but reads "
                f"{len(changed)} answers otherwise than they were read then (the first "
                f"{changed[0]}). Bump GRADING_CODE_VERSION or RULES_VERSION and judge the change "
                "as a revision of its own (stage A step 2)"
            )
    return held


def load_versions(
    out_dir: Path, answers: Sequence[v5.Answer], current: Mapping[str, str]
) -> dict[str, list[PassFile]]:
    """Every version's passes, by label, in the order the versions were judged."""
    found: dict[str, list[PassFile]] = {}
    for path in sorted(out_dir.glob("stage-a-*.pass1.json")):
        label = path.name.removeprefix("stage-a-").removesuffix(".pass1.json")
        passes = [
            load_pass(out_dir, label, n, answers, current)
            for n in range(1, PASSES + 1)
            if pass_path(out_dir, label, n).exists()
        ]
        for later in passes[1:]:
            if later.versions != passes[0].versions or later.gap_types != passes[0].gap_types:
                raise Refused(f"the passes of {label} are not all one version's")
        found[label] = passes
    return dict(sorted(found.items(), key=lambda kv: kv[1][0].judged_at))


def check_label(label: str) -> None:
    if not LABEL.fullmatch(label) or label == CHOICE:
        raise Refused(
            f"{label!r} cannot name a version: use letters, digits, '-' and '_', and not {CHOICE!r}"
        )


# ---- the replay check (no call) ---------------------------------------------------------------


def with_cited_names(
    found: Assessment, output: Mapping[str, Any], names: ServiceNames
) -> Assessment:
    raw = output.get("citations")
    urls = [
        str(c.get("url", "")) for c in (raw if isinstance(raw, list) else []) if isinstance(c, dict)
    ]
    return found.model_copy(update={"cited_names": names.cited(urls)})


def replay_check(
    answers: Sequence[v5.Answer], names: ServiceNames, out_dir: Path
) -> dict[str, Any]:
    """The code in place over judge v5's revision 1 passes, with the cited services' names filled
    in from the stored citations; every pre-registered figure is compared, and the record is
    written (once per code version) when all of them hold. Nothing is judged."""
    by_key = {a.key: a for a in answers}
    read_passes: list[dict[str, AnswerVerdicts]] = []
    assessed: list[dict[str, Assessment]] = []
    moved: list[int] = []
    for salt in v5.SALTS:
        path = v5.pass_path(v5.OUT_DIR, V5_REV1, salt)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if {str(n["key"]) for n in raw["answers"]} != set(by_key):
            raise Refused(f"{path} was judged on another tuning set than the store holds now")
        found = {
            key: with_cited_names(Assessment.model_validate(a), by_key[key].output, names)
            for key, a in raw["assessments"].items()
        }
        held = {key: read_verdicts(a) for key, a in found.items()}
        assessed.append(found)
        read_passes.append(held)
        moved.append(
            sum(held[REPLAY_MOVED].cells[cell_name(t, s)].fully_supported for t, s in CELLS)
        )
    v2_reading = read(answers, [p for _, p in v2_passes(answers)])
    figures: dict[str, Any] = {"cells": {}}
    problems: list[str] = []
    for t, s in CELLS:
        reading = read(answers, [claim_verdicts(p, t, s) for p in read_passes])
        capped = (reading.capped.false_passes, reading.capped.false_fails)
        total = int(reading.total_errors * PASSES)
        figures["cells"][cell_name(t, s)] = {
            "capped_93": {"false_passes": capped[0], "false_fails": capped[1]},
            "total_125": total,
            "named_32": reading.named.errors_and_judge_errors,
            "kept_passes": reading.kept_passes,
            "names_count_as_a_rule": _names_rule_moves(assessed, t, s, by_key, names),
        }
        if capped != REPLAY_93[t]:
            problems.append(f"{cell_name(t, s)}: errors on the 93 are {capped}, not {REPLAY_93[t]}")
        if total != REPLAY_125[(t, s)]:
            problems.append(
                f"{cell_name(t, s)}: {total} errors on the 125, not {REPLAY_125[(t, s)]}"
            )
        for key, n in reading.kept_passes.items():
            miss = key.endswith(KNOWN_MISS) or (key == REPLAY_KEPT_75_FAILS and t == 74)
            want = 0 if miss else PASSES
            if n != want:
                problems.append(f"{cell_name(t, s)}: kept answer {key} passes {n} of 3, not {want}")
    v2_total = int(v2_reading.total_errors * PASSES)
    if v2_total != REPLAY_V2_125:
        problems.append(f"v2 makes {v2_total} errors on the 125, not {REPLAY_V2_125}")
    if moved != [len(CELLS)] * PASSES:
        problems.append(f"{REPLAY_MOVED} passes at {moved} of the nine cells per salt, not all")
    passed = not problems
    record = {
        "checked_at": _now(),
        "grading_code_version": GRADING_CODE_VERSION,
        "rules_version": RULES_VERSION,
        "service_names_sha256": names.sha256,
        "passes_read": [v5.pass_path(v5.OUT_DIR, V5_REV1, salt).as_posix() for salt in v5.SALTS],
        "v2_errors_on_125": v2_total,
        "moved_answer": {"key": REPLAY_MOVED, "cells_passed_per_salt": moved},
        "figures": figures,
        "problems": problems,
        "passed": passed,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPLAY_FILE).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record


def _names_rule_moves(
    passes: Sequence[Mapping[str, Assessment]],
    t: int,
    s: int | None,
    answers: Mapping[str, v5.Answer],
    names: ServiceNames,
) -> dict[str, int]:
    """Reported only (judge v6, change 4): were the names count a rule failing every answer with
    a claim that holds a name no quote states, how many passing verdicts it would move at this
    cell, and of those how many it would get right (the answer is labeled unsupported) and wrong.
    The counts are computed here, as the grader computes them at assess time."""
    moved = right = 0
    for found in passes:
        for key, assessment in found.items():
            if assessment.error is not None or not assessment.verdict(t, s).fully_supported:
                continue
            answer = answers[key]
            raw = answer.output.get("citations")
            quotes = [
                cj.without_links(str(c.get("quote", "")))
                for c in (raw if isinstance(raw, list) else [])
                if isinstance(c, dict)
            ]
            counts = cj._names_in_claims(
                answer.case.question,
                str(answer.output.get("answer", "")),
                assessment.claims,
                quotes,
                assessment.cited_names,
            )
            if any(counts):
                moved += 1
                right += int(not answer.supported)
    return {"passes_moved_to_fail": moved, "right": right, "wrong": moved - right}


def replay_record(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / REPLAY_FILE
    if not path.is_file():
        return None
    held: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return held


def require_replay(out_dir: Path, names: ServiceNames, first_version: bool) -> None:
    """The first paid pass needs a passed replay check of the code in place; a later version
    needs a passed check of some code version (its own code is read against the saved passes of
    its label as every pass is loaded)."""
    held = replay_record(out_dir)
    if held is None or not held.get("passed"):
        raise Refused(
            "the replay check comes first and must pass: uv run python "
            "scripts/judge_compare_v6.py replay-check (stage A, 'The versions, as planned')"
        )
    same = (
        held.get("grading_code_version") == GRADING_CODE_VERSION
        and held.get("rules_version") == RULES_VERSION
        and held.get("service_names_sha256") == names.sha256
    )
    if first_version and not same:
        raise Refused(
            "the replay check on record is another code version's: run replay-check again with "
            "the code in place before the first paid pass"
        )


# ---- the pilot, folded into the first version's passes --------------------------------------


def pilot_draw(answers: Sequence[v5.Answer]) -> list[v5.Answer]:
    drawn: list[v5.Answer] = []
    for run_id, n in zip(v5.RUNS, PILOT_PER_RUN, strict=True):
        ranked = sorted(
            (a for a in answers if a.run_id == run_id),
            key=lambda a: sha256_text(f"{PILOT_SEED}/{a.run_id}/{a.case.id}"),
        )
        if len(ranked) < n:
            raise Refused(f"{run_id} has {len(ranked)} tuning answers, fewer than the pilot's {n}")
        drawn += ranked[:n]
    return drawn


def flipped(passes: Sequence[Pass], t: int, s: int | None) -> tuple[int, int]:
    per_pass = [claim_verdicts(p.verdicts, t, s) for p in passes]
    full = [
        {v[key] for v in per_pass}
        for key in per_pass[0]
        if all(v[key] is not None for v in per_pass)
    ]
    return sum(len(found) > 1 for found in full), len(full)


def pilot(
    label: str,
    answers: Sequence[v5.Answer],
    llm: LLMClient,
    config: JudgeConfig,
    names: ServiceNames,
    *,
    judge_round: spend.Round,
    out_dir: Path,
    workers: int,
    tick: Callable[[], None],
) -> dict[str, Any]:
    """Step 1, once, on the first version judged: the 20 drawn answers under its three salts. The
    record is written once, so a stop it records stands. The judgings are cached, so the version's
    passes that follow read them for free."""
    path = out_dir / PILOT_FILE
    if path.exists():
        raise Refused(f"{path} exists: the pilot runs once (stage A step 1)")
    chosen = pilot_draw(answers)
    judgings = len(chosen) * PASSES
    ledger = judge_round.ledger()
    budget = ledger.check("pilot", judgings * judge_round.estimate_per_answer_usd)
    meter = MeteredLLM(llm, config.model.pricing, budget)
    passes: list[Pass] = []
    try:
        for n in range(1, PASSES + 1):
            passes.append(
                judge_pass(
                    meter, config, names, chosen, pass_salt(label, n), workers=workers, tick=tick
                )
            )
    finally:
        done = len(passes) == PASSES
        spent = ledger.record("pilot" if done else "pilot (stopped early)", meter)
    usage = Usage()
    for judged in passes:
        usage = usage.add(judged.usage)
    nominal = usage.cost_usd(config.model.pricing)
    assert nominal is not None
    cost = nominal / judgings
    stop = cost > judge_round.stop_factor * judge_round.estimate_per_answer_usd
    record = {
        "judged_at": _now(),
        "label": label,
        "draw": {"seed": PILOT_SEED, "per_run": dict(zip(v5.RUNS, PILOT_PER_RUN, strict=True))},
        "answers": [a.key for a in chosen],
        "salts": [pass_salt(label, n) for n in range(1, PASSES + 1)],
        "versions": versions(llm, config, names),
        "cost_per_answer_usd": cost,
        "estimate_per_answer_usd": judge_round.estimate_per_answer_usd,
        "stop_factor": judge_round.stop_factor,
        "stop": stop,
        "flips": {
            cell_name(t, s): dict(zip(("flipped", "of"), flipped(passes, t, s), strict=True))
            for t, s in CELLS
        },
        "spent_usd": spent.usd,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(record, indent=1) + "\n")
    return record


# ---- stage A ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    label: str
    version_order: int
    t: int
    s: int | None
    reading: Reading
    gates: Gates
    build_check: bool
    dropped_span_flips: int
    fail_rates: list[float]  # the share of the 125 failed, per pass
    judge_errors_by_pass: list[int]

    @property
    def name(self) -> str:
        return f"{self.label} {cell_name(self.t, self.s)}"

    @property
    def spec(self) -> str:
        return f"{self.label}:{self.t}:{_s(self.s)}"


def grid(
    label: str,
    version_order: int,
    answers: Sequence[v5.Answer],
    passes: Sequence[Mapping[str, AnswerVerdicts]],
    v2: Reading,
) -> list[Cell]:
    cells = []
    judge_errors = [sum(v.judge_error for v in p.values()) for p in passes]
    for t, s in CELLS:
        per_pass = [claim_verdicts(p, t, s) for p in passes]
        reading = read(answers, per_pass)
        cells.append(
            Cell(
                label=label,
                version_order=version_order,
                t=t,
                s=s,
                reading=reading,
                gates=gates(reading, v2, answers, judge_errors),
                build_check=build_check(reading, v2),
                dropped_span_flips=sum(dropped_span_flips(p, t, s) for p in passes),
                fail_rates=[
                    sum(v is not True for v in verdicts.values()) / len(verdicts)
                    for verdicts in per_pass
                ],
                judge_errors_by_pass=judge_errors,
            )
        )
    return cells


def candidate_order(cells: Iterable[Cell]) -> list[Cell]:
    return sorted(
        (c for c in cells if c.gates.eligible),
        key=lambda c: step4_key(c.reading, c.t, c.s, c.version_order),
    )


@dataclass(frozen=True)
class StageAResult:
    label: str
    gap_types: bool
    paths: list[Path]
    judged_at: str
    spent: list[SpendEntry]
    pilot: dict[str, Any] | None  # the pilot's record, when this call ran it
    cells: list[Cell]
    order: list[Cell]
    v2: Reading
    counts: JudgingCounts  # over the three passes
    versions_counted: int
    choice: Path | None


def stage_a(
    label: str,
    gap_types: bool,
    answers: Sequence[v5.Answer],
    llm: LLMClient,
    config: JudgeConfig,
    names: ServiceNames,
    *,
    judge_round: spend.Round,
    out_dir: Path,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> StageAResult:
    check_label(label)
    config = config.model_copy(update={"gap_types": gap_types})
    current = versions(llm, config, names)
    saved = load_versions(out_dir, answers, current)
    choice = out_dir / CHOICE_FILE
    spent: list[SpendEntry] = []
    pilot_record: dict[str, Any] | None = None
    if label in saved and (
        saved[label][0].versions != current or saved[label][0].gap_types != gap_types
    ):
        raise Refused(
            f"{pass_path(out_dir, label, 1)} holds another version's pass: a revision is judged "
            "under a label of its own"
        )
    if label not in saved or len(saved[label]) < PASSES:  # new, or stopped before its third pass
        same = [o for o, held in saved.items() if held[0].versions == current and o != label]
        if same:
            raise Refused(f"this version was judged already, as {same[0]!r}: one label per version")
        if choice.exists():
            raise Refused(f"{choice} exists: the choice is made, and no version is judged after it")
        if label not in saved and len(saved) >= MAX_VERSIONS:
            raise Refused(
                f"{len(saved)} versions are judged already, and the design allows the first draft "
                "and three revisions: another is a new proposal (stage A step 2)"
            )
        require_replay(out_dir, names, first_version=not saved)
        if spend.pilot_record(out_dir) is None:
            if saved:
                raise Refused("no pilot record, yet a version is judged: the folder is not whole")
            pilot_record = pilot(
                label,
                answers,
                llm,
                config,
                names,
                judge_round=judge_round,
                out_dir=out_dir,
                workers=workers,
                tick=tick,
            )
        per_answer = judge_round.pilot_cost()  # refused after a pilot that stopped the work
        step = f"stage-a {label}"
        ledger = judge_round.ledger()
        budget = ledger.check(step, PASSES * len(answers) * per_answer)
        meter = MeteredLLM(llm, config.model.pricing, budget)
        done = 0
        try:
            for n in range(1, PASSES + 1):
                if pass_path(out_dir, label, n).exists():
                    done += 1
                    continue
                judged = judge_pass(
                    meter, config, names, answers, pass_salt(label, n), workers=workers, tick=tick
                )
                save_pass(out_dir, label, n, judged, current, gap_types, answers)
                done += 1
        finally:
            spent.append(
                ledger.record(step if done == PASSES else f"{step} (stopped early)", meter)
            )
        saved = load_versions(out_dir, answers, current)
    v2 = read(answers, [p for _, p in v2_passes(answers)])
    by_version = {
        held_label: grid(held_label, n, answers, [p.verdicts for p in passes], v2)
        for n, (held_label, passes) in enumerate(saved.items())
    }
    passes = saved[label]
    counts = JudgingCounts()
    for p in passes:
        counts = counts.add(JudgingCounts.model_validate(p.counts))
    return StageAResult(
        label=label,
        gap_types=gap_types,
        paths=[pass_path(out_dir, label, n) for n in range(1, PASSES + 1)],
        judged_at=passes[0].judged_at,
        spent=spent,
        pilot=pilot_record,
        cells=by_version[label],
        order=candidate_order(c for cells in by_version.values() for c in cells),
        v2=v2,
        counts=counts,
        versions_counted=len(saved),
        choice=choice if choice.exists() else None,
    )


# ---- the choice -------------------------------------------------------------------------------


class FreshOnly:
    """An LLMClient for the choice salts: a reply the call cache serves is refused, since a
    choice pass must be a new sample (stage A step 5)."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        resp = self.llm.create(request, salt=salt)
        if resp.cache_hit:
            raise Refused(
                f"a claim-judge call under the choice salt {salt.split('#')[0]!r} was served by "
                "the call cache: the choice reads fresh samples only (stage A step 5)"
            )
        return resp


def parse_candidate(spec: str) -> tuple[str, int, int | None]:
    ts = {str(t): t for t in T_GRID}
    ss = {_s(s): s for s in S_GRID}
    label, _, cell = spec.partition(":")
    t, _, s = cell.partition(":")
    if LABEL.fullmatch(label) and t in ts and s in ss:
        return label, ts[t], ss[s]
    raise Refused(f"{spec!r} is not LABEL:T:S, with t in {T_GRID} and s off, 49 or 59")


def choice_salt(n: int) -> str:
    return f"{ROUND}-{CHOICE}-{n}"


def choice_path(out_dir: Path, label: str, n: int) -> Path:
    return out_dir / f"stage-a-{label}.{CHOICE}{n}.json"


@dataclass(frozen=True)
class Candidate:
    label: str
    t: int
    s: int | None
    version_order: int
    version: str
    reading: Reading
    gates: Gates
    build_check: bool
    counts: JudgingCounts
    dropped_span_flips: int

    @property
    def name(self) -> str:
        return f"{self.label} {cell_name(self.t, self.s)}"


@dataclass(frozen=True)
class ChoiceResult:
    ordered: list[Candidate]
    v2: Reading
    chosen: Candidate | None
    stop: str | None
    spent: SpendEntry
    path: Path


def choose(
    specs: Sequence[str],
    answers: Sequence[v5.Answer],
    llm: LLMClient,
    config: JudgeConfig,
    names: ServiceNames,
    *,
    judge_round: spend.Round,
    out_dir: Path,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> ChoiceResult:
    """Steps 5 and 6: the candidates under the three choice salts, every call fresh, then the
    choice on those means and the stops. The choice is made once."""
    path = out_dir / CHOICE_FILE
    if path.exists():
        raise Refused(f"{path} exists: the choice is made once")
    wanted = [parse_candidate(spec) for spec in specs]
    v2 = read(answers, [p for _, p in v2_passes(answers)])
    saved: dict[str, list[PassFile]] = {}
    for gap_types in (False, True):
        current = versions(llm, config.model_copy(update={"gap_types": gap_types}), names)
        saved |= {
            label: passes
            for label, passes in load_versions(out_dir, answers, current).items()
            if passes[0].gap_types == gap_types
        }
    saved = dict(sorted(saved.items(), key=lambda kv: kv[1][0].judged_at))
    order = candidate_order(
        cell
        for n, (label, passes) in enumerate(saved.items())
        for cell in grid(label, n, answers, [p.verdicts for p in passes], v2)
    )
    if not order:
        raise Refused("no cell of any version is eligible, so the round stops (stage A step 6)")
    # The second candidate is the first cell after the first that differs in version or in t.
    first = order[0]
    second = next((c for c in order[1:] if (c.label, c.t) != (first.label, first.t)), None)
    expected = [first] if second is None else [first, second]
    if len(wanted) != len(expected) or set(wanted) != {(c.label, c.t, c.s) for c in expected}:
        raise Refused(
            "the candidates are step 4's first cell and the first after it that differs in "
            "version or in t: " + " ".join(c.spec for c in expected)
        )
    labels = list(dict.fromkeys(label for label, _, _ in wanted))
    for label in labels:
        gap_types = saved[label][0].gap_types
        current = versions(llm, config.model_copy(update={"gap_types": gap_types}), names)
        if saved[label][0].versions != current:
            raise Refused(
                f"the choice passes of {label} need that version's claim judge, and the code now "
                "is another version: put it back and run choose again"
            )
    per_answer = judge_round.pilot_cost()
    todo = [
        (label, n)
        for label in labels
        for n in range(1, PASSES + 1)
        if not choice_path(out_dir, label, n).exists()
    ]
    step = "choose " + " ".join(specs)
    ledger = judge_round.ledger()
    budget = ledger.check(step, len(todo) * len(answers) * per_answer)
    meter = MeteredLLM(FreshOnly(llm), config.model.pricing, budget)
    done = False
    try:
        for label, n in todo:
            cfg = config.model_copy(update={"gap_types": saved[label][0].gap_types})
            judged = judge_pass(
                meter, cfg, names, answers, choice_salt(n), workers=workers, tick=tick
            )
            held = PassFile(
                label=label,
                salt=judged.salt,
                gap_types=cfg.gap_types,
                judged_at=_now(),
                versions=saved[label][0].versions,
                answers=notes(answers),
                verdicts=judged.verdicts,
                counts=judged.counts.model_dump(),
                assessments={k: a.model_dump(mode="json") for k, a in judged.assessments.items()},
                usage=judged.usage,
            )
            with choice_path(out_dir, label, n).open("x", encoding="utf-8") as fh:
                fh.write(held.model_dump_json(indent=1) + "\n")
        done = True
    finally:
        spent = ledger.record(step if done else f"{step} (stopped early)", meter)
    if meter.calls and meter.uncached.model_calls != meter.calls:
        raise Refused(
            "the choice step's calls were not all sent to the API; its ledger entry says so"
        )
    candidates = []
    for label, t, s in wanted:
        fresh = [
            PassFile.model_validate_json(choice_path(out_dir, label, n).read_text(encoding="utf-8"))
            for n in range(1, PASSES + 1)
        ]
        per_pass = [claim_verdicts(p.verdicts, t, s) for p in fresh]
        reading = read(answers, per_pass)
        judge_errors = [sum(v.judge_error for v in p.verdicts.values()) for p in fresh]
        counts = JudgingCounts()
        for p in fresh:
            counts = counts.add(JudgingCounts.model_validate(p.counts))
        candidates.append(
            Candidate(
                label=label,
                t=t,
                s=s,
                version_order=list(saved).index(label),
                version=saved[label][0].versions[cell_name(t, s)],
                reading=reading,
                gates=gates(reading, v2, answers, judge_errors),
                build_check=build_check(reading, v2),
                counts=counts,
                dropped_span_flips=sum(dropped_span_flips(p.verdicts, t, s) for p in fresh),
            )
        )
    ordered = sorted(candidates, key=lambda c: step4_key(c.reading, c.t, c.s, c.version_order))
    chosen = next((c for c in ordered if c.gates.eligible), None)
    stop = (
        None
        if chosen is not None
        else (
            "no candidate is eligible on its fresh means: the total gate, the caps on the 93, the "
            "kept rule and the judge-error cap (stage A step 6)"
        )
    )
    record = {
        "decided_at": _now(),
        "salts": [choice_salt(n) for n in range(1, PASSES + 1)],
        "candidates": [_candidate_json(c, answers) for c in ordered],
        "v2": {"version": ONLINE_JUDGE_VERSION, **_reading_json(v2, answers)},
        "chosen": None if chosen is None else {"label": chosen.label, "t": chosen.t, "s": chosen.s},
        "stop": stop,
        "frozen": (
            None
            if chosen is None
            else {
                "label": chosen.label,
                "t": chosen.t,
                "s": chosen.s,
                "gap_types": saved[chosen.label][0].gap_types,
                "version": chosen.version,
            }
        ),
    }
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(record, indent=1) + "\n")
    return ChoiceResult(ordered, v2, chosen, stop, spent, path)


def _counts_json(c: Counts) -> dict[str, Any]:
    totals = {
        "false_passes": c.false_passes,
        "false_fails": c.false_fails,
        "errors": c.errors,
        "judge_errors": c.judge_errors,
        "errors_and_judge_errors": c.errors_and_judge_errors,
    }
    return {
        "answers": c.answers,
        "passes": c.passes,
        "totals": totals,
        "means": {name: float(c.mean(n)) for name, n in totals.items()},
    }


def _reading_json(r: Reading, answers: Sequence[v5.Answer]) -> dict[str, Any]:
    return {
        "capped_93": _counts_json(r.capped),
        "named_32": _counts_json(r.named),
        "all_125": _counts_json(r.total),
        "total_errors_per_pass": float(r.total_errors),
        "v5_others_106": _counts_json(r.v5_others),
        "v5_named_19": _counts_json(r.v5_named),
        "kept_passes": r.kept_passes,
        "keeps": r.keeps(answers),
    }


def _candidate_json(c: Candidate, answers: Sequence[v5.Answer]) -> dict[str, Any]:
    return {
        "label": c.label,
        "t": c.t,
        "s": c.s,
        "version": c.version,
        **_reading_json(c.reading, answers),
        "gates": asdict(c.gates),
        "eligible": c.gates.eligible,
        "build_check": c.build_check,
        "counts": c.counts.model_dump(),
        "dropped_span_flips": c.dropped_span_flips,
    }


# ---- what the commands print ------------------------------------------------------------------


def _num(x: Fraction) -> str:
    return str(x.numerator) if x.denominator == 1 else f"{float(x):.2f}"


def _yes(ok: bool) -> str:
    return "yes" if ok else "no"


def _spent(entries: Sequence[SpendEntry], ledger: SpendLedger) -> str:
    usd = sum(e.usd for e in entries)
    calls = sum(e.calls for e in entries)
    uncached = sum(e.uncached_calls for e in entries)
    return (
        f"spent ${usd:.4f}: {uncached} of {calls} calls sent to the API, the rest from the call "
        f"cache. The round has spent ${ledger.total_usd():.2f} of its ${ledger.cap_usd:.2f} cap."
    )


def _row(name: str, r: Reading, extra: Sequence[str]) -> str:
    parts = [f"{name:<22}"]
    for counts, with_je in ((r.capped, False), (r.named, True), (r.total, True)):
        errors = counts.errors_and_judge_errors if with_je else counts.errors
        parts.append(
            f"{_num(counts.mean(counts.false_passes)):>6}{_num(counts.mean(counts.false_fails)):>6}"
            f"{_num(counts.mean(counts.judge_errors)):>5}{_num(counts.mean(errors)):>7}"
        )
    parts.append(f"{_num(r.total_errors):>7}")
    return "  ".join(parts) + "".join(f"  {e}" for e in extra)


def _header(extra: Sequence[str]) -> list[str]:
    head = f"{'FP':>6}{'FF':>6}{'JE':>5}{'errors':>7}"
    return [
        f"  {'':<22}  {'capped (93)':>24}  {'named (32)':>24}  {'all (125)':>24}  {'total':>7}",
        f"  {'':<22}  {head}  {head}  {head}  {'gate':>7}" + "".join(f"  {e}" for e in extra),
    ]


COLUMNS = (
    "FP, FF: false passes and false fails per pass; JE: judge errors (no verdict). errors: on the "
    "capped count (the 93) FP and FF; on the named and on all 125, FP, FF and JE. total gate: "
    "errors on the 93 plus errors and JE on the 32, per pass, which must be fewer than v2's."
)


def _counts_line(c: JudgingCounts) -> str:
    def share(n: int, of: int) -> str:
        return f"{n} of {of} ({n / of:.0%})" if of else "0 of 0"

    gaps = (
        f"none {c.gaps_none}, implication {c.gaps_implication}, scope {c.gaps_scope}, term "
        f"{c.gaps_term}, cell {c.gaps_cell}, other {c.gaps_other}"
    )
    return (
        f"of the {c.judgings} judgings: retried {share(c.retried, c.judgings)}; judge errors "
        f"{share(c.judge_errors, c.judgings)}; support entries dropped "
        f"{share(c.dropped, c.support_entries)}; searches naming words not in the answer "
        f"{share(c.search_missing, c.judgings)}. Gap types: {gaps}; rule fails {c.rule_fails}; "
        f"downgrades {c.downgrades}; terms in a quote {c.terms_quoted}, in none "
        f"{c.terms_unquoted}. Own names cleared by code {c.names_cleared} (the skeptic scored "
        f"{c.names_cleared_skeptic_low} of them under t). Names in claims that no quote states: "
        f"{c.names_unquoted} in {c.claims_with_names_unquoted} claims."
    )


def render_stage_a(result: StageAResult, ledger: SpendLedger) -> str:
    lines = [
        f"stage A, version {result.label!r} (gap types {'on' if result.gap_types else 'off'}): "
        f"the claim judge on the {result.v2.total.answers} tuning answers under three salts "
        f"({'judged now' if result.spent else 'read back; judged ' + result.judged_at}). "
        f"Versions so far: {result.versions_counted} of the {MAX_VERSIONS} the design allows.",
    ]
    if result.pilot is not None:
        p = result.pilot
        lines.append(
            f"pilot (step 1): {len(p['answers'])} answers drawn with the seed {PILOT_SEED!r} under "
            f"this version's three salts; cost per answer ${p['cost_per_answer_usd']:.4f} against "
            f"the ${p['estimate_per_answer_usd']:.3f} estimate (stop at {p['stop_factor']} times)."
            + (" STOP: the work stops and the budget is revisited." if p["stop"] else "")
        )
    if result.spent:
        lines.append(_spent(result.spent, ledger))
    lines += [_counts_line(result.counts), ""]
    lines += _header(("caps", "keeps", "JE cap", "eligible", "build check", "fail rates"))
    lines.append(_row("v2 (3 passes)", result.v2, ("", "", "", "", "", "")))
    for c in result.cells:
        rates = "/".join(f"{r:.0%}" for r in c.fail_rates)
        lines.append(
            _row(
                cell_name(c.t, c.s),
                c.reading,
                (
                    _yes(c.gates.caps),
                    _yes(c.gates.keeps),
                    _yes(c.gates.judge_error_cap),
                    _yes(c.gates.eligible),
                    _yes(c.build_check),
                    rates,
                ),
            )
        )
    lines += ["", COLUMNS]
    lines.append(
        "caps: on the 93, errors at most v2's plus 2 and false fails at most v2's plus 1. keeps: "
        "each of the six kept answers (the known miss docs-mix-058 aside) passes on two of three. "
        "JE cap: judge errors at most 2% of the 125 in every pass. eligible: the total gate, the "
        "caps, keeps and the JE cap. build check (reported, a prediction): fewer errors than v2 on "
        "the 32. fail rates: the share of the 125 failed, per pass (v2's stored pass: 70.4%)."
    )
    known = [k for k in result.cells[0].reading.kept_passes if k.endswith(KNOWN_MISS)]
    for key in known:
        per_cell = ", ".join(
            f"{cell_name(c.t, c.s)}: {c.reading.kept_passes[key]}" for c in result.cells
        )
        lines.append(f"known miss {key}, passes of three: {per_cell}")
    lines.append("")
    if not result.order:
        lines.append("no cell of any version is eligible yet (step 3).")
    else:
        lines.append("candidate order (step 4), over every version's eligible cells:")
        first = result.order[0]
        second = next(
            (c for c in result.order[1:] if (c.label, c.t) != (first.label, first.t)), None
        )
        for n, c in enumerate(result.order, 1):
            mark = "   <- candidate" if c in (first, second) else ""
            lines.append(
                f"  {n}. {c.name}: {_num(c.reading.total_errors)} errors per pass on the 125, "
                f"{_num(c.reading.capped.mean(c.reading.capped.errors))} on the 93{mark}"
            )
        specs = [first.spec] + ([second.spec] if second else [])
        if result.choice is None:
            lines.append(
                "after the last revision: uv run python scripts/judge_compare_v6.py choose "
                + " ".join(specs)
            )
    if result.choice is not None:
        lines.append(f"the choice is made (steps 5 and 6), in {result.choice}.")
    return "\n".join(lines)


def render_choice(result: ChoiceResult, ledger: SpendLedger) -> str:
    lines = [
        f"the choice (stage A steps 5 and 6) on three fresh passes under {choice_salt(1)} to "
        f"{choice_salt(PASSES)}; every count is a mean over the passes.",
        _spent([result.spent], ledger),
        "",
    ]
    lines += _header(("caps", "keeps", "JE cap", "eligible", "build check"))
    lines.append(_row("v2 (3 passes)", result.v2, ("", "", "", "", "")))
    for c in result.ordered:
        lines.append(
            _row(
                c.name,
                c.reading,
                (
                    _yes(c.gates.caps),
                    _yes(c.gates.keeps),
                    _yes(c.gates.judge_error_cap),
                    _yes(c.gates.eligible),
                    _yes(c.build_check),
                ),
            )
        )
    lines += ["", COLUMNS]
    for c in result.ordered:
        lines.append(f"{c.name}, {_counts_line(c.counts)}")
    lines.append("")
    if result.chosen is None:
        lines.append(f"STOP: {result.stop}")
    else:
        c = result.chosen
        lines.append(
            f"FROZEN: {c.label}, t = {c.t}, s = {_s(c.s)}: the claim judge's version {c.version}. "
            f"Build check (prediction): {'held' if c.build_check else 'missed'}. In "
            f"configs/judge-v6.yaml: support_threshold: {c.t}, skeptic_threshold: "
            f"{'null' if c.s is None else c.s}, and gap_types as the version has it."
        )
    lines.append(f"Saved in {result.path}.")
    return "\n".join(lines)


def render_replay(record: Mapping[str, Any]) -> str:
    lines = [
        "replay check: the code in place over judge v5's revision 1 passes, no call made "
        f"(grading code {record['grading_code_version']}, rules {record['rules_version']}, "
        f"service names {record['service_names_sha256'][:12]})."
    ]
    for name, cell in record["figures"]["cells"].items():
        capped = cell["capped_93"]
        kept_short = ", ".join(f"{k.split('/')[-1]}:{n}" for k, n in cell["kept_passes"].items())
        lines.append(
            f"  {name}: 93 {capped['false_passes']}/{capped['false_fails']}; 125: "
            f"{cell['total_125']}; 32: {cell['named_32']}; kept {kept_short}"
        )
    lines.append(
        f"  v2 on the 125: {record['v2_errors_on_125']}; {REPLAY_MOVED.split('/')[-1]} passes "
        f"{record['moved_answer']['cells_passed_per_salt']} of {len(CELLS)} cells per salt"
    )
    if record["passed"]:
        lines.append(
            "PASSED: every pre-registered figure is reproduced. The first paid pass may run."
        )
    else:
        lines.append("FAILED:")
        lines += [f"  {p}" for p in record["problems"]]
    return "\n".join(lines)


# ---- the commands -----------------------------------------------------------------------------

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


@dataclass(frozen=True)
class Setup:
    answers: list[v5.Answer]
    llm: LLMClient
    claim: Config
    names: ServiceNames
    judge_round: spend.Round


def _setup() -> Setup:
    load_dotenv()
    claim = load_config(CLAIM_CONFIG)
    if claim.judge.grader_name != ROUND:
        raise Refused(f"{CLAIM_CONFIG} names the grader {claim.judge.grader_name!r}, not {ROUND!r}")
    cases = {case.id: case for case in load_suite(v5.DEV_SUITE)}
    store = ResultsStore(claim.paths.db, read_only=True)
    try:
        answers = v5.load_tuning_set(store, cases)
    finally:
        store.close()
    check_tuning_set(answers)
    llm = AnthropicClient(CallCache(claim.paths.cache_dir))
    return Setup(answers, llm, claim, ServiceNames.load(), spend.round_for(ROUND))


@contextmanager
def _ticker() -> Iterator[Callable[[], None]]:
    columns = (TextColumn("judging"), BarColumn(), TextColumn("{task.completed:.0f} answers"))
    with Progress(*columns, transient=True) as progress:
        task = progress.add_task("judging", total=None)
        yield lambda: progress.advance(task)


def _print_or_refuse(run: Callable[[], str]) -> None:
    try:
        print(run())
    except (Refused, v5.Refused, PilotRefusal, SpendCapError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc


@app.command("replay-check")
def replay_check_command() -> None:
    """The code in place over judge v5's saved passes, no call made; its record gates stage A."""

    def run() -> str:
        s = _setup()
        record = replay_check(s.answers, s.names, OUT_DIR)
        text = render_replay(record)
        if not record["passed"]:
            print(text, file=sys.stderr)
            raise typer.Exit(code=2)
        return text

    _print_or_refuse(run)


@app.command("stage-a")
def stage_a_command(
    label: Annotated[str, typer.Argument(help="This version's name, e.g. draft or rev1.")],
    gap_types: Annotated[
        bool, typer.Option("--gap-types/--no-gap-types", help="Ask for a gap type per claim.")
    ] = False,
) -> None:
    """Steps 1 to 4: this version on the 125 under three fresh salts (the pilot first, once)."""

    def run() -> str:
        s = _setup()
        with _ticker() as tick:
            result = stage_a(
                label,
                gap_types,
                s.answers,
                s.llm,
                s.claim.judge,
                s.names,
                judge_round=s.judge_round,
                out_dir=OUT_DIR,
                workers=s.claim.run.workers,
                tick=tick,
            )
        return render_stage_a(result, s.judge_round.ledger())

    _print_or_refuse(run)


@app.command("choose")
def choose_command(
    first: Annotated[str, typer.Argument(help="The first candidate, LABEL:T:S.")],
    second: Annotated[str | None, typer.Argument(help="The second, LABEL:T:S, if any.")] = None,
) -> None:
    """Steps 5 and 6: the candidates under three fresh salts, then the choice. Costs money."""

    def run() -> str:
        s = _setup()
        specs = [first] if second is None else [first, second]
        with _ticker() as tick:
            result = choose(
                specs,
                s.answers,
                s.llm,
                s.claim.judge,
                s.names,
                judge_round=s.judge_round,
                out_dir=OUT_DIR,
                workers=s.claim.run.workers,
                tick=tick,
            )
        return render_choice(result, s.judge_round.ledger())

    _print_or_refuse(run)


if __name__ == "__main__":
    app()
