"""Stage A of the claim judge's validation (docs/plans/judge-v5.md, approved 2026-10-28).

    uv run python scripts/judge_compare_v5.py pilot
    uv run python scripts/judge_compare_v5.py stage-a LABEL
    uv run python scripts/judge_compare_v5.py choose LABEL_A:T_A:S_A [LABEL_B:T_B:S_B]

pilot    Step 1, run once. 20 tuning answers drawn with a fixed seed, 7, 7 and 6 from the three
         dev runs, each under three salts: the cost per answer, how often a verdict flips
         between salts, how often a reply is retried, ends in a judge error, or loses a support
         entry to the span check, and how often a verdict fails only because of such a loss.
stage-a  Steps 2 to 4. The 125 tuning answers once, with the claim judge as the code has it now:
         one version (the first draft or a revision), saved under LABEL. Prints the grid of t
         and s against v2's stored verdicts, the eligible cells, and the candidate order over
         every version judged so far. A version is judged once; asked again, it is read back.
         The version the pilot judged is the first draft, so it counts among the four, and any
         other is a revision. Once choose has made the choice, no new version is judged.
choose   Steps 5 and 6. The two candidates of that order, each a stage-a label and a cell (s is
         off, 70 or 90), and v2, under two more salts: the frozen t and s on three-pass means,
         or the stop that ends the round.

Every step that judges costs money. Each call goes through the call cache, so what was judged
before is free, and each paid step is checked against the round's spend ledger before its first
call, stopped during it when it has spent what the cap leaves, and recorded in the ledger after,
however it ends. Each pass is saved with the verdicts its version's code read at every cell when
it was judged, and every later step reads those: the code in place reads again only the passes of
its own version, and refuses one it reads otherwise. The results database is opened read-only and
closed before any call, and no grade is stored: what the script finds goes to results/judge-v5/.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Protocol

import typer
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from rich.progress import BarColumn, Progress, TextColumn

from agent_evals.config import Config, JudgeConfig, load_config
from agent_evals.graders.claim_judge import S_GRID, T_GRID, Assessment, ClaimJudgeGrader
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.cache import CallCache, sha256_text
from agent_evals.harness.llm import AnthropicClient, LLMClient
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.spend import (
    DEFAULT_PATH,
    ESTIMATE_PER_ANSWER_USD,
    PILOT_FILE,
    STOP_FACTOR,
    MeteredLLM,
    PilotRefusal,
    SpendCapError,
    SpendEntry,
    SpendLedger,
    pilot_cost,
    pilot_record,
)
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import AgentResult, Case, Usage

# The tuning set: the author's judge-dev labels on attempt 1 of the three dev runs.
RUNS = (
    "docs-20261011-223111-702e87",  # the first dev run
    "docs-20261016-214710-02abb7",  # the champion's
    "docs-20261017-174739-1c0e8e",  # iteration 4's
)
LABELER = "aleena"
LABEL_SIDE = "judge-dev"
V2 = "judge"  # the validated judge's grader name; its stored verdicts are v2's first pass
DEV_SUITE = Path("suites/docs/dev.jsonl")
CLAIM_CONFIG = Path("configs/judge-v5.yaml")
V2_CONFIG = Path("configs/default.yaml")
OUT_DIR = DEFAULT_PATH.parent  # results/judge-v5, beside the round's spend ledger
# The design's appendix, frozen 2026-10-28: every case it quotes, paraphrases or built a pattern
# from. The online ones are on no tuning answer; they stay so that this is the appendix's list.
NAMED_CASES = frozenset(
    {
        "online-d01-085",
        "online-d02-071",
        "online-d03-068",
        "online-d04-030",
        "online-d04-032",
        "online-d04-072",
        "docs-ai-001-p2",
        "docs-ai-004-p3",
        "docs-ai-007",
        "docs-chg-004",
        "docs-chg-007",
        "docs-chg-008",
        "docs-chg-022",
        "docs-core-025",
        "docs-core-034",
        "docs-int-014",
        "docs-int-020",
        "docs-int-029",
        "docs-mix-022",
        "docs-mix-024",
        "docs-mix-049",
        "docs-mix-050",
        "docs-mix-057",
        "docs-mix-078",
        "docs-mix-090",
    }
)
# What the design counts on the tuning set. Any other set is refused, since the rules name these.
TUNING_ANSWERS = 125
NAMED_ANSWERS = 19
KEPT_ANSWERS = 1  # named answers labeled supported that v2 passes: a new judge must pass them too
SALTS = ("judge", "judge-s2", "judge-s3")  # a pass each; the first is the stored grades' salt
# Step 1's draw (the clarifications of 2026-10-28): from each dev run, in the order of RUNS, this
# many of its tuning answers, the first in the order of a hash of the seed, the run id and the
# case id. Fixed, so the same tuning set always gives the same draw.
PILOT_PER_RUN = (7, 7, 6)
PILOT_ANSWERS = sum(PILOT_PER_RUN)  # 20
PILOT_SEED = "judge-v5-pilot"
# The grid (step 2), in its order; T_GRID and S_GRID are the claim judge's own, which refuses
# any other t or s. An s of None turns the skeptic's search off.
CELLS: tuple[tuple[int, int | None], ...] = tuple((t, s) for t in T_GRID for s in S_GRID)
S_ORDER = {None: 0, 90: 1, 70: 2}  # step 4's tie order: s off before 90 before 70
# The first draft, which the pilot judges (step 1), and at most three revisions (step 2).
MAX_VERSIONS = 4
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")  # it names files, so no dots or slashes
CHOICE = "choice"
CHOICE_FILE = "stage-a-choice.json"

Verdicts = dict[str, bool | None]  # answer key -> fully supported; None: no verdict


class Refused(Exception):
    """The script stops: the data, a file or a request is not what the design names."""


class TuningStore(Protocol):
    """The reads the script makes of the results store. It never writes."""

    def run_info(self, run_id: str) -> dict[str, Any]: ...

    def labels(self, run_id: str, labeler: str | None = None) -> list[dict[str, Any]]: ...

    def grade_reasons(
        self, run_id: str, grader: str
    ) -> dict[tuple[str, int], dict[str, tuple[float, str]]]: ...

    def all_attempts(self, run_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Answer:
    """A tuning answer: one stored attempt-1 answer, its label and v2's stored verdict."""

    run_id: str
    case: Case
    output: dict[str, Any]
    supported: bool  # the label: fully supported
    v2_passed: bool  # v2's stored judge_supported

    @property
    def key(self) -> str:
        return f"{self.run_id}/{self.case.id}"

    @property
    def named(self) -> bool:
        return self.case.id in NAMED_CASES

    @property
    def result(self) -> AgentResult:
        return AgentResult(output=self.output)


def load_tuning_set(store: TuningStore, cases: Mapping[str, Case]) -> list[Answer]:
    """The tuning answers, in case-id order: the author's judge-dev labels with a fully supported
    value, on attempt 1 of the three dev runs, joined to v2's stored verdict. Refuses a case
    outside the dev split, and any set but the one the design counts."""
    answers: list[Answer] = []
    for run_id in RUNS:
        info = store.run_info(run_id)
        note = info["manifest"].get(V2, {})
        if info["online"] or note.get("version") != ONLINE_JUDGE_VERSION:
            raise Refused(
                f"{run_id} must be a dev run with v2's ({ONLINE_JUDGE_VERSION}) verdicts stored "
                f"under {V2!r}; its note there is {note}"
            )
        if note.get("salt", SALTS[0]) != SALTS[0]:
            raise Refused(
                f"v2's stored verdicts on {run_id} are under salt {note['salt']!r}: as the first "
                f"pass they must be under {SALTS[0]!r}, or a later pass would repeat them"
            )
        v2 = store.grade_reasons(run_id, V2)
        stored = {(a["case_id"], a["attempt"]): a for a in store.all_attempts(run_id)}
        for label in store.labels(run_id, LABELER):
            key = (label["case_id"], label["attempt"])
            if (
                label["label_split"] != LABEL_SIDE
                or label["supported"] is None
                or label["attempt"] != 1
                or "judge_supported" not in v2.get(key, {})
            ):
                continue
            case = cases.get(label["case_id"])
            if case is None or case.split != "dev":
                raise Refused(
                    f"{run_id}/{label['case_id']} is not a dev-split case of {DEV_SUITE}: stage "
                    "A tunes on dev labels only"
                )
            answers.append(
                Answer(
                    run_id=run_id,
                    case=case,
                    output=stored[key]["output"],
                    supported=bool(label["supported"]),
                    v2_passed=v2[key]["judge_supported"][0] >= 1.0,
                )
            )
    answers.sort(key=lambda a: (a.case.id, a.run_id))
    shape = (len(answers), sum(a.named for a in answers), len(to_keep(answers)))
    if shape != (TUNING_ANSWERS, NAMED_ANSWERS, KEPT_ANSWERS):
        raise Refused(
            f"the tuning set has {shape[0]} answers, {shape[1]} of them on named cases and "
            f"{shape[2]} of those labeled supported and passed by v2; the design counts "
            f"{TUNING_ANSWERS}, {NAMED_ANSWERS} and {KEPT_ANSWERS}"
        )
    return answers


def to_keep(answers: Iterable[Answer]) -> list[Answer]:
    """The named answers labeled supported that v2's stored verdict passes (step 3)."""
    return [a for a in answers if a.named and a.supported and a.v2_passed]


def stored_v2(answers: Iterable[Answer]) -> Verdicts:
    return {a.key: a.v2_passed for a in answers}


def _s(s: int | None) -> str:
    return "off" if s is None else str(s)


def cell_name(t: int, s: int | None) -> str:
    return f"t={t} s={_s(s)}"


CELL_NAMES = frozenset(cell_name(t, s) for t, s in CELLS)


class CellVerdict(BaseModel):
    """One answer's verdicts at one cell of the grid."""

    fully_supported: bool
    core_supported: bool
    dropped_span_flip: bool  # it fails only because the span check dropped a support entry


class AnswerVerdicts(BaseModel):
    """One answer under one salt, as the claim judge's code read it when it was judged: its
    verdicts at each of the nine cells, or a judge error, which has a verdict at none."""

    judge_error: bool
    cells: dict[str, CellVerdict] = Field(default_factory=dict)  # by cell name


def read_verdicts(found: Assessment) -> AnswerVerdicts:
    """An assessment's verdicts at the nine cells, as the code in place reads them, each with
    whether it fails only because the span check dropped a support entry: the claim judge's own
    rule (Assessment.dropped_span_flip), which `evals judge` counts by too."""
    if found.error is not None:
        return AnswerVerdicts(judge_error=True)
    cells: dict[str, CellVerdict] = {}
    for t, s in CELLS:
        v = found.verdict(t, s)
        cells[cell_name(t, s)] = CellVerdict(
            fully_supported=v.fully_supported,
            core_supported=v.core_supported,
            dropped_span_flip=found.dropped_span_flip(t, s),
        )
    return AnswerVerdicts(judge_error=False, cells=cells)


def claim_verdicts(verdicts: Mapping[str, AnswerVerdicts], t: int, s: int | None) -> Verdicts:
    """The claim judge's fully supported verdicts at one cell, as saved; None for a judge error."""
    name = cell_name(t, s)
    return {
        key: None if found.judge_error else found.cells[name].fully_supported
        for key, found in verdicts.items()
    }


def dropped_span_flips(verdicts: Mapping[str, AnswerVerdicts], t: int, s: int | None) -> int:
    """Answers that fail at this cell only because the span check dropped a support entry."""
    name = cell_name(t, s)
    return sum(
        not found.judge_error and found.cells[name].dropped_span_flip for found in verdicts.values()
    )


@dataclass(frozen=True)
class Counts:
    """A judge's errors against the labels on some answers, summed over its passes. A judge error
    is no verdict, so it is neither kind of error (the design's rule everywhere); it is counted
    on its own, and as an error only where the design says so (errors_and_judge_errors)."""

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
        """The errors with each judge error counted as one: an answer a judge cannot judge is not
        one it gets right. Step 4 ranks the named cases by this, and step 6's build check reads
        it, for both judges (the clarifications of 2026-10-28)."""
        return self.errors + self.judge_errors

    def mean(self, total: int) -> Fraction:
        """A total as its mean per pass: exact, so no cap is missed by a rounding."""
        return Fraction(total, self.passes)


def count(answers: Sequence[Answer], passes: Sequence[Verdicts]) -> Counts:
    false_passes = false_fails = judge_errors = 0
    for verdicts in passes:
        for a in answers:
            verdict = verdicts[a.key]
            if verdict is None:
                judge_errors += 1
            elif verdict and not a.supported:
                false_passes += 1
            elif not verdict and a.supported:
                false_fails += 1
    return Counts(len(answers), len(passes), false_passes, false_fails, judge_errors)


def keeps(answers: Iterable[Answer], passes: Sequence[Verdicts]) -> bool:
    """Every named answer labeled supported that v2 passes is passed: on a single pass, or on at
    least two of three. A judge error is not a pass."""
    need = len(passes) // 2 + 1
    return all(sum(v[a.key] is True for v in passes) >= need for a in to_keep(answers))


@dataclass(frozen=True)
class Reading:
    """One judge (a version at one cell, or v2) over its passes."""

    others: Counts  # the answers outside the named cases
    named: Counts
    keeps: bool


def read(answers: Sequence[Answer], passes: Sequence[Verdicts]) -> Reading:
    return Reading(
        others=count([a for a in answers if not a.named], passes),
        named=count([a for a in answers if a.named], passes),
        keeps=keeps(answers, passes),
    )


def eligible(judge: Reading, v2: Reading) -> bool:
    """Step 3: outside the named cases, errors at most v2's plus 2, of which false fails at most
    v2's plus 1, and the named answer labeled supported that v2 passes is passed. Over three
    passes, the counts are means against v2's."""
    mine, theirs = judge.others, v2.others
    return (
        mine.mean(mine.errors) <= theirs.mean(theirs.errors) + 2
        and mine.mean(mine.false_fails) <= theirs.mean(theirs.false_fails) + 1
        and judge.keeps
    )


def step4_key(
    reading: Reading, t: int, s: int | None, version_order: int
) -> tuple[Fraction, Fraction, int, int, int]:
    """Step 4's order: fewest errors on the named cases, each judge error counted as one (the
    build working, not evidence), then fewest errors outside them, then lower t, then s off
    before 90 before 70. The design stops there; a tie left over goes to the version judged
    first."""
    named, others = reading.named, reading.others
    return (
        named.mean(named.errors_and_judge_errors),
        others.mean(others.errors),
        t,
        S_ORDER[s],
        version_order,
    )


@dataclass(frozen=True)
class Cell:
    """One cell of a version's grid, read from its single pass."""

    label: str  # the version, as stage-a named it
    version_order: int  # its place in the order the versions were judged
    t: int
    s: int | None
    reading: Reading
    eligible: bool
    dropped_span_flips: int

    @property
    def name(self) -> str:
        return f"{self.label} {cell_name(self.t, self.s)}"

    @property
    def spec(self) -> str:
        return f"{self.label}:{self.t}:{_s(self.s)}"


def candidate_order(cells: Iterable[Cell]) -> list[Cell]:
    """Every eligible cell of every version, best first; the first two are the candidates."""
    return sorted(
        (c for c in cells if c.eligible),
        key=lambda c: step4_key(c.reading, c.t, c.s, c.version_order),
    )


def grid(
    label: str,
    version_order: int,
    answers: Sequence[Answer],
    verdicts: Mapping[str, AnswerVerdicts],
) -> list[Cell]:
    """A version's nine cells, from its single pass's saved verdicts, against v2's stored
    verdicts (step 2)."""
    v2 = read(answers, [stored_v2(answers)])
    cells = []
    for t, s in CELLS:
        reading = read(answers, [claim_verdicts(verdicts, t, s)])
        flips = dropped_span_flips(verdicts, t, s)
        cells.append(Cell(label, version_order, t, s, reading, eligible(reading, v2), flips))
    return cells


def at_cell(config: JudgeConfig, t: int, s: int | None) -> JudgeConfig:
    return config.model_copy(update={"support_threshold": t, "skeptic_threshold": s})


def versions(llm: LLMClient, config: JudgeConfig) -> dict[str, str]:
    """The claim judge's version at each cell. t and s are part of a version, so each cell is a
    judge of its own; equal maps mean the same prompts and code."""
    return {cell_name(t, s): ClaimJudgeGrader(llm, at_cell(config, t, s)).version for t, s in CELLS}


def _nothing() -> None:
    return None


@dataclass(frozen=True)
class Pass:
    """The claim judge on every answer once, under one salt: what it found, the verdicts its code
    read from that at every cell when it was judged, and the usage of its calls."""

    salt: str
    assessments: dict[str, Assessment]
    verdicts: dict[str, AnswerVerdicts]
    usage: Usage


def judge_pass(
    llm: LLMClient,
    config: JudgeConfig,
    answers: Sequence[Answer],
    salt: str,
    *,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> Pass:
    grader = ClaimJudgeGrader(llm, config, salt=salt)

    def one(answer: Answer) -> tuple[str, Assessment]:
        found = grader.assess(answer.case, answer.result)
        if found is None:  # labeled for support, so it has citations: there is always something
            raise Refused(f"{answer.key}: the claim judge finds nothing to judge in it")
        return answer.key, found

    assessments: dict[str, Assessment] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, found in pool.map(one, answers):
            assessments[key] = found
            tick()
    verdicts = {key: read_verdicts(found) for key, found in assessments.items()}
    return Pass(salt, assessments, verdicts, grader.usage)


def v2_pass(
    llm: LLMClient,
    config: JudgeConfig,
    answers: Sequence[Answer],
    salt: str,
    *,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> Verdicts:
    """v2's fully supported verdicts once, under one salt: None where the reply held no verdict,
    which `evals judge` would store as a grader_error."""
    grader = JudgeGrader(llm, config, salt=salt)

    def one(answer: Answer) -> tuple[str, bool | None]:
        try:
            grades = grader.grade(answer.case, answer.result)
        except ValueError:  # a reply without a valid verdict; an API failure still raises
            return answer.key, None
        return answer.key, {g.metric: g.passed for g in grades}.get("judge_supported")

    verdicts: Verdicts = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, verdict in pool.map(one, answers):
            verdicts[key] = verdict
            tick()
    return verdicts


@dataclass(frozen=True)
class Events:
    """What the checks counted over some judgings, each one answer under one salt."""

    judgings: int
    retried: int  # judgings with a reply asked again
    judge_errors: int
    support_entries: int
    dropped: int  # support entries the span check dropped
    with_dropped: int  # judgings that lost at least one
    search_missing: int  # searches whose words were not in the answer, even on the retry


def events(assessments: Iterable[Assessment]) -> Events:
    found = list(assessments)
    return Events(
        judgings=len(found),
        retried=sum(a.retries > 0 for a in found),
        judge_errors=sum(a.error is not None for a in found),
        support_entries=sum(len(c.support) for a in found for c in a.claims),
        dropped=sum(a.dropped_support for a in found),
        with_dropped=sum(a.dropped_support > 0 for a in found),
        search_missing=sum(a.search_span_missing for a in found),
    )


def added(found: Iterable[Events]) -> Events:
    """The events of several passes, added up."""
    held = list(found)
    return Events(**{f.name: sum(getattr(e, f.name) for e in held) for f in fields(Events)})


class AnswerNote(BaseModel):
    """A tuning answer as a pass file records it, so a later read can check the store."""

    key: str
    named: bool
    supported: bool
    v2_passed: bool


class PassFile(BaseModel):
    """One pass of one claim judge version over the tuning answers, written once. Its verdicts
    and events are what that version's code read when it was judged, and every later step reads
    them. The assessments are kept as judged, and only the code of their own version reads them
    again (load_pass)."""

    label: str
    salt: str
    judged_at: str
    versions: dict[str, str]  # the claim judge's version at each cell
    answers: list[AnswerNote]
    verdicts: dict[str, AnswerVerdicts]
    events: Events
    assessments: dict[str, dict[str, Any]]  # each answer's Assessment, as JSON
    usage: Usage  # every call of the pass, cached or not
    grid: list[dict[str, Any]] = Field(default_factory=list)  # a first pass's cells, as judged


def _now() -> str:
    # To the microsecond: the versions are ordered by when their first pass was judged.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def notes(answers: Iterable[Answer]) -> list[AnswerNote]:
    return [
        AnswerNote(key=a.key, named=a.named, supported=a.supported, v2_passed=a.v2_passed)
        for a in answers
    ]


def pass_path(out_dir: Path, label: str, salt: str) -> Path:
    """stage-a-LABEL.json holds a version's first pass, and its later passes sit beside it."""
    if salt == SALTS[0]:
        return out_dir / f"stage-a-{label}.json"
    return out_dir / f"stage-a-{label}.{salt}.json"


def save_pass(
    out_dir: Path,
    label: str,
    judged: Pass,
    judge_versions: dict[str, str],
    answers: Sequence[Answer],
    cells: Sequence[Cell] = (),
) -> None:
    held = PassFile(
        label=label,
        salt=judged.salt,
        judged_at=_now(),
        versions=judge_versions,
        answers=notes(answers),
        verdicts=judged.verdicts,
        events=events(judged.assessments.values()),
        assessments={key: a.model_dump(mode="json") for key, a in judged.assessments.items()},
        usage=judged.usage,
        grid=[_cell_json(c) for c in cells],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with pass_path(out_dir, label, judged.salt).open("x", encoding="utf-8") as fh:  # once
        fh.write(held.model_dump_json(indent=1) + "\n")


def load_pass(
    out_dir: Path,
    label: str,
    salt: str,
    answers: Sequence[Answer],
    current: Mapping[str, str],
) -> PassFile:
    """A saved pass, checked against the tuning set it was judged on. Its saved verdicts are what
    every later step reads. When it is the version of the code in place (`current`), that code
    reads its assessments again, and must find the verdicts saved."""
    path = pass_path(out_dir, label, salt)
    held = PassFile.model_validate_json(path.read_text(encoding="utf-8"))
    if (held.label, held.salt) != (label, salt):
        raise Refused(f"{path} holds {held.label!r} under salt {held.salt!r}")
    keys = {a.key for a in answers}
    if (
        held.answers != notes(answers)
        or held.verdicts.keys() != keys
        or held.assessments.keys() != keys
    ):
        raise Refused(f"{path} was judged on another tuning set than the store holds now")
    if any(
        v.cells.keys() != (set() if v.judge_error else CELL_NAMES) for v in held.verdicts.values()
    ):
        raise Refused(f"{path} does not hold each answer's verdicts at the nine cells")
    if held.versions == current:
        _check_reading(path, held)
    return held


def _check_reading(path: Path, held: PassFile) -> None:
    """The code in place is the version this pass was judged with, so it must read the pass's
    assessments as they were read then. A difference is a change the version does not cover."""
    change = (
        f"{path}: the code in place has the version this pass was judged with, but reads it "
        "otherwise than it was read then, so the version does not cover a change. For a change to "
        "the grader, bump RULES_VERSION or GRADING_CODE_VERSION in graders/claim_judge.py and "
        "judge it as a revision of its own (stage A step 2)"
    )
    try:
        now = {key: Assessment.model_validate(raw) for key, raw in held.assessments.items()}
    except ValidationError as exc:
        raise Refused(f"{change}: its assessments no longer parse") from exc
    changed = sorted(
        key for key, found in now.items() if read_verdicts(found) != held.verdicts[key]
    )
    if changed:
        raise Refused(f"{change}: {len(changed)} answers read otherwise, the first {changed[0]}")


def load_versions(
    out_dir: Path, answers: Sequence[Answer], current: Mapping[str, str]
) -> dict[str, PassFile]:
    """Every version's first pass, by label, in the order the versions were judged."""
    found = []
    for path in out_dir.glob("stage-a-*.json"):
        label = path.name.removeprefix("stage-a-").removesuffix(".json")
        if label != CHOICE and "." not in label:  # not the choice, nor a later pass
            found.append(load_pass(out_dir, label, SALTS[0], answers, current))
    return {held.label: held for held in sorted(found, key=lambda held: held.judged_at)}


def pilot_versions(out_dir: Path) -> dict[str, str] | None:
    """The claim judge's version at each cell when the pilot judged it, or None before the pilot.
    The pilot judges the first draft (steps 1 and 2), so its version counts among the four."""
    held = pilot_record(out_dir)
    if held is None:
        return None
    found = held.get("versions")
    if not isinstance(found, dict) or set(found) != CELL_NAMES:
        raise Refused(
            f"{out_dir / PILOT_FILE} does not record the version the pilot judged at each of "
            "the nine cells"
        )
    return {str(cell): str(version) for cell, version in found.items()}


def distinct(found: Iterable[Mapping[str, str] | None]) -> list[Mapping[str, str]]:
    """The different versions among these, in their order, a None left out."""
    kept: list[Mapping[str, str]] = []
    for version in found:
        if version is not None and version not in kept:
            kept.append(version)
    return kept


def check_label(label: str) -> None:
    if not LABEL.fullmatch(label) or label == CHOICE:
        raise Refused(
            f"{label!r} cannot name a version: use letters, digits, '-' and '_', and not {CHOICE!r}"
        )


def parse_candidate(spec: str) -> tuple[str, int, int | None]:
    """LABEL:T:S, a stage-a label and a cell: t 50, 70 or 85, and s off, 70 or 90."""
    ts = {str(t): t for t in T_GRID}
    ss = {_s(s): s for s in S_GRID}
    label, _, cell = spec.partition(":")
    t, _, s = cell.partition(":")
    if LABEL.fullmatch(label) and t in ts and s in ss:
        return label, ts[t], ss[s]
    raise Refused(f"{spec!r} is not LABEL:T:S, with t 50, 70 or 85 and s off, 70 or 90")


def pilot_draw(answers: Sequence[Answer]) -> list[Answer]:
    """Step 1's answers: from each dev run, its tuning answers in the order of a hash of the
    seed, the run id and the case id, the first 7, 7 and 6 (PILOT_PER_RUN, in the order of RUNS).
    Not the case-id order, which gave 20 answers from one service area and no named case."""
    drawn: list[Answer] = []
    for run_id, n in zip(RUNS, PILOT_PER_RUN, strict=True):
        ranked = sorted((a for a in answers if a.run_id == run_id), key=_draw_key)
        if len(ranked) < n:
            raise Refused(f"{run_id} has {len(ranked)} tuning answers, fewer than the pilot's {n}")
        drawn += ranked[:n]
    return drawn


def _draw_key(answer: Answer) -> str:
    return sha256_text(f"{PILOT_SEED}/{answer.run_id}/{answer.case.id}")


@dataclass(frozen=True)
class PilotResult:
    drawn: list[str]  # the answers' keys, in the draw's order
    cost_per_answer: float  # every call priced, whether or not the call cache served it
    flips: dict[str, tuple[int, int]]  # cell -> (answers that flip, answers with three verdicts)
    # cell -> (judgings that fail only because of a dropped span, judgings with a verdict)
    dropped_span_flips: dict[str, tuple[int, int]]
    events: Events
    stop: bool
    spent: SpendEntry
    path: Path


def flipped(passes: Sequence[Pass], t: int, s: int | None) -> tuple[int, int]:
    """Answers whose fully supported verdict differs between the passes, and the answers with a
    verdict in every pass."""
    per_pass = [claim_verdicts(p.verdicts, t, s) for p in passes]
    full = [
        {verdicts[key] for verdicts in per_pass}
        for key in per_pass[0]
        if all(verdicts[key] is not None for verdicts in per_pass)
    ]
    return sum(len(found) > 1 for found in full), len(full)


def pilot_dropped(passes: Sequence[Pass], t: int, s: int | None) -> tuple[int, int]:
    """Judgings that fail at this cell only because of a dropped span, over the passes, and the
    judgings with a verdict."""
    with_verdict = sum(not v.judge_error for p in passes for v in p.verdicts.values())
    return sum(dropped_span_flips(p.verdicts, t, s) for p in passes), with_verdict


def pilot(
    answers: Sequence[Answer],
    llm: LLMClient,
    config: JudgeConfig,
    *,
    ledger: SpendLedger,
    out_dir: Path,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> PilotResult:
    """Step 1, once: the drawn answers, each under the three salts. Its record is written once,
    so a stop it records stands; a second pilot is refused before any call."""
    path = out_dir / PILOT_FILE
    if path.exists():
        raise Refused(
            f"{path} exists: the pilot runs once, and what it measured, a stop included, stands "
            "(stage A step 1)"
        )
    chosen = pilot_draw(answers)
    judgings = len(chosen) * len(SALTS)
    budget = ledger.check("pilot", judgings * ESTIMATE_PER_ANSWER_USD)
    meter = MeteredLLM(llm, config.model.pricing, budget)
    passes: list[Pass] = []
    try:
        for salt in SALTS:
            passes.append(judge_pass(meter, config, chosen, salt, workers=workers, tick=tick))
    finally:
        done = len(passes) == len(SALTS)
        spent = ledger.record("pilot" if done else "pilot (stopped early)", meter)
    usage = Usage()
    for judged in passes:
        usage = usage.add(judged.usage)
    nominal = usage.cost_usd(config.model.pricing)
    assert nominal is not None  # the meter refused a config without pricing
    cost = nominal / judgings
    flips = {cell_name(t, s): flipped(passes, t, s) for t, s in CELLS}
    dropped = {cell_name(t, s): pilot_dropped(passes, t, s) for t, s in CELLS}
    found = events(a for judged in passes for a in judged.assessments.values())
    stop = cost > STOP_FACTOR * ESTIMATE_PER_ANSWER_USD
    record = {
        "judged_at": _now(),
        "draw": {
            "seed": PILOT_SEED,
            "rule": (
                "from each dev run, its tuning answers in the order of the sha256 of "
                "'seed/run_id/case_id', the first per_run"
            ),
            "per_run": dict(zip(RUNS, PILOT_PER_RUN, strict=True)),
        },
        "answers": [a.key for a in chosen],
        "salts": list(SALTS),
        "versions": versions(llm, config),
        "cost_per_answer_usd": cost,
        "estimate_per_answer_usd": ESTIMATE_PER_ANSWER_USD,
        "stop": stop,
        "flips": {name: {"flipped": n, "of": of} for name, (n, of) in flips.items()},
        "dropped_span_flips": {name: {"flipped": n, "of": of} for name, (n, of) in dropped.items()},
        "events": asdict(found),
        "assessments": {
            judged.salt: {key: a.model_dump(mode="json") for key, a in judged.assessments.items()}
            for judged in passes
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:  # once, as the check above says
        fh.write(json.dumps(record, indent=1) + "\n")
    return PilotResult([a.key for a in chosen], cost, flips, dropped, found, stop, spent, path)


@dataclass(frozen=True)
class StageAResult:
    label: str
    path: Path
    judged_at: str
    spent: SpendEntry | None  # None when the pass was read back, not judged
    cells: list[Cell]  # this version's grid
    order: list[Cell]  # step 4, over every version's eligible cells
    v2: Reading  # v2's stored verdicts
    events: Events
    first_draft: bool | None  # it is the version the pilot judged; None with no pilot record
    versions_counted: int  # the pilot's and every saved pass's, each different version once
    choice: Path | None  # the choice's record, once step 5 has made it


def stage_a(
    label: str,
    answers: Sequence[Answer],
    llm: LLMClient,
    config: JudgeConfig,
    *,
    ledger: SpendLedger,
    out_dir: Path,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> StageAResult:
    """Steps 2 to 4 for the claim judge as the code has it now: its single pass over the tuning
    answers, judged once and read back after, its grid, and the candidate order over every
    version judged so far, each read from its own saved verdicts. The pilot judged the first
    draft, so its version counts among the four the design allows, and a version that differs
    from it is a revision. Once the choice is made (step 5), no new version is judged; a version
    judged before it is still read back."""
    check_label(label)
    current = versions(llm, config)
    saved = load_versions(out_dir, answers, current)
    drafted = pilot_versions(out_dir)
    choice = out_dir / CHOICE_FILE
    spent = None
    if label in saved:
        if saved[label].versions != current:
            raise Refused(
                f"{pass_path(out_dir, label, SALTS[0])} holds another version's pass: a revision "
                "is judged under a label of its own"
            )
    else:
        same = [other for other, held in saved.items() if held.versions == current]
        if same:
            raise Refused(
                f"this version was judged already, as {same[0]!r}: each version is judged once "
                "(stage A step 2)"
            )
        if choice.exists():
            raise Refused(
                f"{choice} exists: the choice is made, and no version is judged after it (stage "
                "A steps 5 and 6). A version judged before it can still be read back"
            )
        counted = distinct([drafted, *(held.versions for held in saved.values())])
        if current not in counted and len(counted) >= MAX_VERSIONS:
            raise Refused(
                f"{len(counted)} versions are judged already, the first draft the pilot judged "
                "counted among them, and the design allows the first draft and three revisions: "
                "another is a new proposal (stage A step 2)"
            )
        per_answer = pilot_cost(out_dir)
        step = f"stage-a {label}"
        budget = ledger.check(step, len(answers) * per_answer)
        meter = MeteredLLM(llm, config.model.pricing, budget)
        judged = None
        try:
            judged = judge_pass(meter, config, answers, SALTS[0], workers=workers, tick=tick)
        finally:
            done = judged is not None
            spent = ledger.record(step if done else f"{step} (stopped early)", meter)
        cells = grid(label, len(saved), answers, judged.verdicts)
        save_pass(out_dir, label, judged, current, answers, cells)
        saved = load_versions(out_dir, answers, current)
    by_version = {
        held.label: grid(held.label, n, answers, held.verdicts)
        for n, held in enumerate(saved.values())
    }
    held = saved[label]
    return StageAResult(
        label=label,
        path=pass_path(out_dir, label, SALTS[0]),
        judged_at=held.judged_at,
        spent=spent,
        cells=by_version[label],
        order=candidate_order(c for cells in by_version.values() for c in cells),
        v2=read(answers, [stored_v2(answers)]),
        events=held.events,
        first_draft=None if drafted is None else held.versions == drafted,
        versions_counted=len(distinct([drafted, *(h.versions for h in saved.values())])),
        choice=choice if choice.exists() else None,
    )


@dataclass(frozen=True)
class Candidate:
    """A candidate cell on its three passes."""

    label: str
    t: int
    s: int | None
    version_order: int
    version: str  # the claim judge's version at this cell
    reading: Reading
    eligible: bool  # on its means
    events: Events  # over its version's three passes
    dropped_span_flips: int  # at its cell, over the three passes

    @property
    def name(self) -> str:
        return f"{self.label} {cell_name(self.t, self.s)}"


def decide(
    candidates: Sequence[Candidate], v2: Reading
) -> tuple[list[Candidate], Candidate | None, str | None]:
    """Steps 5 and 6: the candidates in step 4's order on their means, the first of them still
    eligible on its means, and the stop that ends the round, if one applies. The build check
    counts each judge error on the named cases as an error, for both judges."""
    ordered = sorted(candidates, key=lambda c: step4_key(c.reading, c.t, c.s, c.version_order))
    chosen = next((c for c in ordered if c.eligible), None)
    if chosen is None:
        return ordered, None, "no candidate is eligible on its three-pass means (stage A step 6)"
    mine, theirs = chosen.reading.named, v2.named
    if mine.mean(mine.errors_and_judge_errors) >= theirs.mean(theirs.errors_and_judge_errors):
        return (
            ordered,
            chosen,
            f"the chosen cell makes no fewer errors than v2 on the {mine.answers} named answers "
            "on its means, with each judge error counted as one, and a judge that does not fix "
            "the cases it was built from is not built right (stage A step 6)",
        )
    return ordered, chosen, None


@dataclass(frozen=True)
class ChoiceResult:
    ordered: list[Candidate]  # in step 4's order on their means
    v2: Reading  # its stored pass and two more
    chosen: Candidate | None
    stop: str | None  # why the round stops; None when the chosen cell is frozen
    code_is_chosen: bool  # the code now is the chosen version
    spent: SpendEntry
    path: Path


def choose(
    specs: Sequence[str],
    answers: Sequence[Answer],
    llm: LLMClient,
    claim_config: JudgeConfig,
    v2_config: JudgeConfig,
    *,
    ledger: SpendLedger,
    out_dir: Path,
    workers: int = 1,
    tick: Callable[[], None] = _nothing,
) -> ChoiceResult:
    """Steps 5 and 6: the candidates, and v2, under two more salts, then the choice on the three
    passes' means and the stops, each pass read from its own saved verdicts. The choice is made
    once."""
    path = out_dir / CHOICE_FILE
    if path.exists():
        raise Refused(f"{path} exists: the choice is made once")
    wanted = [parse_candidate(spec) for spec in specs]
    current = versions(llm, claim_config)
    saved = load_versions(out_dir, answers, current)
    order = candidate_order(
        cell
        for n, held in enumerate(saved.values())
        for cell in grid(held.label, n, answers, held.verdicts)
    )
    if not order:
        raise Refused("no cell of any version is eligible, so the round stops (stage A step 6)")
    if len(wanted) != len(order[:2]) or set(wanted) != {(c.label, c.t, c.s) for c in order[:2]}:
        raise Refused(
            "the candidates are the first two of step 4's order: "
            + " ".join(c.spec for c in order[:2])
        )
    v2_judge = v2_config.model_copy(update={"responsive": False})  # support only: one call each
    if JudgeGrader(llm, v2_judge).version != ONLINE_JUDGE_VERSION:
        raise Refused(f"the v2 config's judge is not v2, {ONLINE_JUDGE_VERSION}")
    per_answer = pilot_cost(out_dir)
    labels = list(dict.fromkeys(label for label, _, _ in wanted))
    missing = [
        (label, salt)
        for label in labels
        for salt in SALTS[1:]
        if not pass_path(out_dir, label, salt).exists()
    ]
    for label in labels:  # the later passes judged already, checked before anything is spent
        for salt in SALTS[1:]:
            if (label, salt) not in missing:
                _same_version(saved[label], load_pass(out_dir, label, salt, answers, current))
    # A version's later passes can only be judged by its own code. When the code is another
    # version's, what it can judge is judged and kept, and choose is run again with the other.
    elsewhere = sorted({label for label, _ in missing if saved[label].versions != current})
    todo = [(label, salt) for label, salt in missing if label not in elsewhere]
    other_code = (
        f"the later passes of {', '.join(elsewhere)} need that version's claim judge, and the "
        "code now is another version: run choose again with that version's graders in place. "
        f"Passes already judged are kept in {out_dir}, and none is judged twice"
    )
    if elsewhere and not todo:
        raise Refused(other_code)
    v2_salts = [] if elsewhere else list(SALTS[1:])
    step = "choose " + " ".join(specs)
    budget = ledger.check(step, (len(todo) + len(v2_salts)) * len(answers) * per_answer)
    # Both judges draw on the one budget the cap leaves this step.
    claim_meter = MeteredLLM(llm, claim_config.model.pricing, budget)
    v2_meter = MeteredLLM(llm, v2_judge.model.pricing, budget)
    v2_passes = [stored_v2(answers)]
    done = False
    try:
        for label, salt in todo:
            judged = judge_pass(
                claim_meter, claim_config, answers, salt, workers=workers, tick=tick
            )
            save_pass(out_dir, label, judged, current, answers)
        for salt in v2_salts:
            v2_passes.append(v2_pass(v2_meter, v2_judge, answers, salt, workers=workers, tick=tick))
        done = True
    finally:
        spent = ledger.record(step if done else f"{step} (stopped early)", claim_meter, v2_meter)
    if elsewhere:
        raise Refused(other_code)
    v2 = read(answers, v2_passes)
    candidates = []
    for label, t, s in wanted:
        first = saved[label]
        passes = [
            first,
            *(load_pass(out_dir, label, salt, answers, current) for salt in SALTS[1:]),
        ]
        for later in passes[1:]:
            _same_version(first, later)
        reading = read(answers, [claim_verdicts(p.verdicts, t, s) for p in passes])
        candidates.append(
            Candidate(
                label=label,
                t=t,
                s=s,
                version_order=list(saved).index(label),
                version=first.versions[cell_name(t, s)],
                reading=reading,
                eligible=eligible(reading, v2),
                events=added(p.events for p in passes),
                dropped_span_flips=sum(dropped_span_flips(p.verdicts, t, s) for p in passes),
            )
        )
    ordered, chosen, stop = decide(candidates, v2)
    code_is_chosen = chosen is not None and saved[chosen.label].versions == current
    record = {
        "decided_at": _now(),
        "salts": list(SALTS),
        "candidates": [
            {
                "label": c.label,
                "t": c.t,
                "s": c.s,
                "version": c.version,
                "others": _counts_json(c.reading.others),
                "named": _counts_json(c.reading.named),
                "keeps": c.reading.keeps,
                "eligible": c.eligible,
                "events": asdict(c.events),
                "dropped_span_flips": {
                    "total": c.dropped_span_flips,
                    "mean": float(c.reading.others.mean(c.dropped_span_flips)),
                },
            }
            for c in ordered
        ],
        "v2": {
            "version": ONLINE_JUDGE_VERSION,
            "others": _counts_json(v2.others),
            "named": _counts_json(v2.named),
        },
        "chosen": None if chosen is None else {"label": chosen.label, "t": chosen.t, "s": chosen.s},
        "stop": stop,
        "frozen": (
            None
            if chosen is None or stop is not None
            else {"label": chosen.label, "t": chosen.t, "s": chosen.s, "version": chosen.version}
        ),
    }
    with path.open("x", encoding="utf-8") as fh:  # the choice is made once
        fh.write(json.dumps(record, indent=1) + "\n")
    return ChoiceResult(ordered, v2, chosen, stop, code_is_chosen, spent, path)


def _same_version(first: PassFile, later: PassFile) -> None:
    if later.versions != first.versions:
        raise Refused(f"the passes of {first.label} are not all one version's")


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


def _cell_json(c: Cell) -> dict[str, Any]:
    return {
        "t": c.t,
        "s": c.s,
        "others": _counts_json(c.reading.others),
        "named": _counts_json(c.reading.named),
        "keeps": c.reading.keeps,
        "eligible": c.eligible,
        "dropped_span_flips": c.dropped_span_flips,
    }


# ---- what the commands print ----------------------------------------------------------------


def _num(x: Fraction) -> str:
    """A count, or a mean over passes to two decimals."""
    return str(x.numerator) if x.denominator == 1 else f"{float(x):.2f}"


def _share(n: int, of: int) -> str:
    return f"{n} of {of} ({n / of:.0%})" if of else "0 of 0"


def _yes(ok: bool) -> str:
    return "yes" if ok else "no"


def _spent(spent: SpendEntry, ledger: SpendLedger) -> str:
    return (
        f"spent ${spent.usd:.4f}: {spent.uncached_calls} of {spent.calls} calls sent to the API, "
        f"the rest from the call cache. The round has spent ${ledger.total_usd():.2f} of its "
        f"${ledger.cap_usd:.2f} cap."
    )


def _counts(c: Counts, *, named: bool) -> str:
    """False passes, false fails and judge errors, then the errors as the design counts them:
    on the named cases each judge error is one, outside them none is."""
    errors = c.errors_and_judge_errors if named else c.errors
    return "".join(
        f"{_num(c.mean(n)):>7}" for n in (c.false_passes, c.false_fails, c.judge_errors, errors)
    )


def _table(rows: Sequence[tuple[str, Reading, Sequence[str]]], extra: Sequence[str]) -> list[str]:
    """Readings side by side: false passes, false fails, judge errors and errors on the answers
    outside the named cases ("others"), the same on the named, then the extra columns."""
    first = rows[0][1]
    others = f"others ({first.others.answers})"
    named = f"named ({first.named.answers})"
    head = "".join(f"{h:>7}" for h in ("FP", "FF", "JE", "errors"))
    lines = [
        f"  {'':<20}{others:>28}   {named:>28}",
        f"  {'':<20}{head}   {head}" + "".join(f"  {h}" for h in extra),
    ]
    for name, reading, values in rows:
        cols = "".join(f"  {v:>{len(h)}}" for v, h in zip(values, extra, strict=True))
        lines.append(
            f"  {name:<20}{_counts(reading.others, named=False)}   "
            f"{_counts(reading.named, named=True)}{cols}"
        )
    return lines


COLUMNS = (
    "FP, FF: false passes and false fails; JE: judge errors, answers with no verdict. errors: "
    "outside the named cases (others) FP and FF, a judge error being in no error count there "
    "(steps 3 and 4); on the named cases FP, FF and JE, since an answer a judge cannot judge is "
    "not one it gets right (steps 4 and 6)."
)


def _events(e: Events) -> str:
    return (
        f"of the {e.judgings} judgings: retried {_share(e.retried, e.judgings)}; judge errors "
        f"{_share(e.judge_errors, e.judgings)}; support entries dropped by the span check "
        f"{_share(e.dropped, e.support_entries)}, in {_share(e.with_dropped, e.judgings)} "
        "judgings; a search naming words not in the answer in "
        f"{_share(e.search_missing, e.judgings)}."
    )


def _by_cell(values: Mapping[str, tuple[int, int]]) -> list[str]:
    lines = []
    for t in T_GRID:
        cells = []
        for s in S_GRID:
            n, of = values[cell_name(t, s)]
            cells.append(f"s={_s(s)}: {n} of {of}")
        lines.append(f"  t={t}   " + "   ".join(cells))
    return lines


def render_pilot(result: PilotResult, ledger: SpendLedger) -> str:
    e = result.events
    per_run = ", ".join(str(n) for n in PILOT_PER_RUN)
    lines = [
        f"pilot (stage A step 1): {len(result.drawn)} tuning answers drawn with the seed "
        f"{PILOT_SEED!r}, {per_run} from the three dev runs, each under the salts "
        f"{', '.join(SALTS)}. Saved in {result.path}, once.",
        _spent(result.spent, ledger),
        f"cost per answer: ${result.cost_per_answer:.4f}, every call priced whether or not the "
        f"call cache served it; the design's estimate is ${ESTIMATE_PER_ANSWER_USD:.2f}.",
    ]
    if result.stop:
        lines.append(
            f"STOP: more than {STOP_FACTOR} times the estimate. The work stops and the budget is "
            "revisited (stage A step 1); the later steps refuse to run."
        )
    lines += [
        _events(e),
        "flips: answers whose fully supported verdict differs between the salts, of those with a "
        "verdict under all three:",
        *_by_cell(result.flips),
        "dropped-span flips: judgings that fail only because the span check dropped a support "
        "entry, of those with a verdict:",
        *_by_cell(result.dropped_span_flips),
    ]
    return "\n".join(lines)


def _standing(result: StageAResult) -> str:
    """Where the version stands among the four the design allows: the first draft, which the
    pilot judged, or a revision of it (steps 1 and 2)."""
    counted = (
        f"{result.versions_counted} of the {MAX_VERSIONS} versions the design allows (the first "
        "draft and three revisions)"
    )
    if result.first_draft is None:
        return f"No pilot record says which version is the first draft. Versions so far: {counted}."
    if result.first_draft:
        return f"This is the first draft, the version the pilot judged. Versions so far: {counted}."
    return (
        "This is a revision: the pilot judged another version, the first draft, so the pilot's "
        "cost and flip rate were measured on that one. Versions so far, the first draft "
        f"included: {counted}."
    )


def render_stage_a(result: StageAResult, ledger: SpendLedger) -> str:
    e = result.events
    how = "judged now" if result.spent else f"read back; judged {result.judged_at}"
    lines = [
        f"stage A, version {result.label!r}: the claim judge once on the {e.judgings} tuning "
        f"answers, under salt {SALTS[0]} ({how}). Saved in {result.path}.",
        _standing(result),
    ]
    if result.spent:
        lines.append(_spent(result.spent, ledger))
    lines += [
        _events(e),
        "",
        *_table(
            [
                ("v2, stored", result.v2, ("", "", "")),
                *(
                    (
                        cell_name(c.t, c.s),
                        c.reading,
                        (_yes(c.reading.keeps), str(c.dropped_span_flips), _yes(c.eligible)),
                    )
                    for c in result.cells
                ),
            ],
            ("keeps", "dropped-span flips", "eligible"),
        ),
        "",
        COLUMNS,
        "keeps: the named answer labeled supported that v2 passes is passed. Dropped-span flips: "
        "fails that would pass had the span check dropped no support entry.",
        "eligible (step 3): outside the named cases, errors at most v2's plus 2, of which false "
        "fails at most v2's plus 1, and it keeps.",
        "",
    ]
    if not result.order:
        lines.append(
            "no cell of any version is eligible yet. If none is after the last revision (at most "
            "three), the round stops (step 6)."
        )
        return "\n".join(lines)
    lines.append("candidate order (step 4), over every version's eligible cells:")
    for n, c in enumerate(result.order, 1):
        named, others = c.reading.named, c.reading.others
        mark = "   <- candidate" if n <= 2 else ""
        lines.append(
            f"  {n}. {c.name}: {_num(named.mean(named.errors_and_judge_errors))} errors on the "
            f"named, judge errors included, {_num(others.mean(others.errors))} outside them{mark}"
        )
    if result.choice is not None:
        lines.append(
            f"the choice is made (steps 5 and 6), in {result.choice}: no version is judged after "
            "it."
        )
    else:
        lines.append(
            "after the last revision: uv run python scripts/judge_compare_v5.py choose "
            + " ".join(c.spec for c in result.order[:2])
        )
    return "\n".join(lines)


def render_choice(result: ChoiceResult, ledger: SpendLedger) -> str:
    lines = [
        f"the choice (stage A steps 5 and 6) on three passes, under the salts {', '.join(SALTS)}; "
        "every count is a mean over the passes.",
        _spent(result.spent, ledger),
        "",
        *_table(
            [
                ("v2", result.v2, ("", "", "")),
                *(
                    (
                        c.name,
                        c.reading,
                        (
                            _yes(c.reading.keeps),
                            _num(c.reading.others.mean(c.dropped_span_flips)),
                            _yes(c.eligible),
                        ),
                    )
                    for c in result.ordered
                ),
            ],
            ("keeps, 2 of 3", "dropped-span flips", "eligible"),
        ),
        "",
        COLUMNS,
        *(f"{c.name}, {_events(c.events)}" for c in result.ordered),
        "",
    ]
    chosen = result.chosen
    if chosen is None or result.stop is not None:
        lines.append(f"STOP: {result.stop}")
    else:
        lines.append(
            f"FROZEN: {chosen.label}, t = {chosen.t}, s = {_s(chosen.s)}: the claim judge's "
            f"version {chosen.version}. In configs/judge-v5.yaml: support_threshold: {chosen.t}, "
            f"skeptic_threshold: {'null' if chosen.s is None else chosen.s}."
        )
        if not result.code_is_chosen:
            lines.append(
                f"The code now is another version than {chosen.label!r}: put that version's "
                "graders back before freezing it."
            )
    lines.append(f"Saved in {result.path}.")
    return "\n".join(lines)


# ---- the commands ---------------------------------------------------------------------------

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


@dataclass(frozen=True)
class Setup:
    answers: list[Answer]
    llm: LLMClient
    claim: Config
    v2: Config
    ledger: SpendLedger


def _setup() -> Setup:
    """What a real run uses: the tuning answers, from the results database opened read-only and
    closed before any call, and the API behind the call cache."""
    load_dotenv()
    claim, v2 = load_config(CLAIM_CONFIG), load_config(V2_CONFIG)
    cases = {case.id: case for case in load_suite(DEV_SUITE)}
    store = ResultsStore(claim.paths.db, read_only=True)
    try:
        answers = load_tuning_set(store, cases)
    finally:
        store.close()
    llm = AnthropicClient(CallCache(claim.paths.cache_dir))
    return Setup(answers, llm, claim, v2, SpendLedger(OUT_DIR / DEFAULT_PATH.name))


@contextmanager
def _ticker() -> Iterator[Callable[[], None]]:
    """A running count of the answers judged, for a step that can take an hour."""
    columns = (TextColumn("judging"), BarColumn(), TextColumn("{task.completed:.0f} answers"))
    with Progress(*columns, transient=True) as progress:
        task = progress.add_task("judging", total=None)
        yield lambda: progress.advance(task)


def _print_or_refuse(run: Callable[[], str]) -> None:
    try:
        print(run())
    except (Refused, PilotRefusal, SpendCapError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc


@app.command("pilot")
def pilot_command() -> None:
    """Step 1, once: 20 tuning answers drawn with a fixed seed, under three salts. Costs money."""

    def run() -> str:
        s = _setup()
        with _ticker() as tick:
            result = pilot(
                s.answers,
                s.llm,
                s.claim.judge,
                ledger=s.ledger,
                out_dir=OUT_DIR,
                workers=s.claim.run.workers,
                tick=tick,
            )
        return render_pilot(result, s.ledger)

    _print_or_refuse(run)


@app.command("stage-a")
def stage_a_command(
    label: Annotated[str, typer.Argument(help="This version's name, e.g. draft or rev1.")],
) -> None:
    """Steps 2 to 4: the 125 tuning answers once with the claim judge as it is now. Costs money
    the first time for each version."""

    def run() -> str:
        s = _setup()
        with _ticker() as tick:
            result = stage_a(
                label,
                s.answers,
                s.llm,
                s.claim.judge,
                ledger=s.ledger,
                out_dir=OUT_DIR,
                workers=s.claim.run.workers,
                tick=tick,
            )
        return render_stage_a(result, s.ledger)

    _print_or_refuse(run)


@app.command("choose")
def choose_command(
    first: Annotated[str, typer.Argument(help="The first candidate, LABEL:T:S.")],
    second: Annotated[
        str | None, typer.Argument(help="The second, LABEL:T:S, unless only one is eligible.")
    ] = None,
) -> None:
    """Steps 5 and 6: the candidates and v2 under two more salts, then the choice. Costs money."""

    def run() -> str:
        s = _setup()
        specs = [first] if second is None else [first, second]
        with _ticker() as tick:
            result = choose(
                specs,
                s.answers,
                s.llm,
                s.claim.judge,
                s.v2.judge,
                ledger=s.ledger,
                out_dir=OUT_DIR,
                workers=s.claim.run.workers,
                tick=tick,
            )
        return render_choice(result, s.ledger)

    _print_or_refuse(run)


if __name__ == "__main__":
    app()
