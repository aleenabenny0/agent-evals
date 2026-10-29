"""The online monitor (phase 7 part 3, docs/plans/phase-7-online-evals.md): is a day worse than
the frozen reference days?

A day's online run becomes per-question values for the plan's alert family. Each metric is
tested one-sided in its bad direction against the pooled reference days (stats/monitor.py), the
family is Holm-corrected, and an alert also needs the effect to reach the metric's practical
floor. Fabricated citations have their own rule outside Holm, because at this volume Holm would
fire only at several times the baseline. Some numbers are only reported. judge_supported joins
the family only once the 30 blind labels have passed, and a day whose answers changed style, or
whose judge errors pass 2% of its cited answers, marks the judge untrusted: an alert still
stands, but a quiet judge_supported does not clear it. A day's verdict is recorded once, in
alerts.json; a later reading of a recorded day is post hoc and goes to post-hoc.json.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, log
from pathlib import Path
from statistics import fmean, median, pstdev
from typing import Any, Literal

from agent_evals.harness.cache import sha256_text
from agent_evals.harness.manifest import file_sha256
from agent_evals.online import judge_pin
from agent_evals.simulate import TrafficQuestion, load_traffic
from agent_evals.stats.monitor import (
    detectable_mean_rise,
    detectable_rate_rise,
    fisher_greater,
    holm,
    permutation_greater,
)
from agent_evals.store.db import ResultsStore, check_record_key
from agent_evals.suites import load_suite
from agent_evals.types import Case

ALPHA = 0.05
FABRICATION_P = 0.01  # the separate safety rule: one-sided Fisher, no floor
TRUST_SHIFT = 0.25  # a style shift past this marks the day's judge_supported untrusted
# Judge errors (the claim judge's judge_error grade, an answer it could not judge) past this
# share of the day's cited answers mark the judge untrusted too (docs/plans/judge-v5.md).
JUDGE_ERROR_SHARE = 0.02
JUDGE_ERROR = "judge_error"
REFERENCE_DAYS = (1, 2, 3, 4, 5)
REFERENCE_PATH = Path("results/online/reference.json")
ALERTS_PATH = Path("results/online/alerts.json")  # each day's verdict, recorded once
POST_HOC_PATH = Path("results/online/post-hoc.json")  # readings of recorded days: never verdicts
JUDGE_CHECK_PATH = Path("results/online/judge-check.json")
JUDGE_SAMPLE_PATH = Path("results/online/judge-check-sample.json")
JUDGE_CHECK_PER_DAY = 6  # 30 answers over the five reference days
JUDGE_CHECK_PASSED_PER_DAY = 3  # of each day's 6: 3 the judge passed, 3 it failed (decision 6)
JUDGE_CHECK_MAX_FALSE_PASSES = 2  # the bar of iteration 4's spot check, over the 15 passes
JUDGE_CHECK_LABELER = "aleena"  # the plan: the author's 30 blind labels, nobody else's
# The first check's two records, the validated judge's, kept on each reference run: its first
# complete score, and its drawn sample with its digests. Another judge's check on the same runs
# keeps its own two, keyed by its grader name (check_record, sample_record).
JUDGE_CHECK_RECORD = "judge_check"
JUDGE_SAMPLE_RECORD = "judge_check_sample"
CANNOT_ALERT = "no day of this n can alert"  # a blind spot with no number: the worst kind
PENDING = "pending: the judge has not yet passed the 30 blind labels"
IN_SCOPE = ("lookup", "multi_step")  # lookups and comparisons: declining one is a failure


def check_record(grader: str = "judge") -> str:
    """The manifest key of a judge's check result: JUDGE_CHECK_RECORD for the validated judge,
    the same name with the grader name after it for any other."""
    return check_record_key(JUDGE_CHECK_RECORD, grader)


def sample_record(grader: str = "judge") -> str:
    """The manifest key of a judge's check sample, keyed like check_record."""
    return check_record_key(JUDGE_SAMPLE_RECORD, grader)


def _is_sample_key(key: str) -> bool:
    """A manifest key holding a judge-check sample, of any judge."""
    return key == JUDGE_SAMPLE_RECORD or key.startswith(JUDGE_SAMPLE_RECORD + ":")


def _check_key_of(sample_key: str) -> str:
    """The key of the result that completes a sample record: the same judge's."""
    return JUDGE_CHECK_RECORD + sample_key.removeprefix(JUDGE_SAMPLE_RECORD)


@dataclass(frozen=True)
class Spec:
    name: str
    kind: Literal["rate", "mean"]
    floor: float  # rate points, a mean difference, or a log ratio
    label: str


ALERT_FAMILY: tuple[Spec, ...] = (
    Spec("judge_supported", "rate", 0.05, "answers the judge found not fully supported"),
    Spec("uncited_answer", "rate", 0.05, "answers with no citation"),
    Spec("in_scope_abstention", "rate", 0.05, "lookups and comparisons declined"),
    Spec("error", "rate", 0.02, "attempts that errored"),
    Spec("model_calls", "mean", 0.5, "model calls per attempt"),
    Spec("latency", "mean", log(1.25), "latency of uncached attempts (log ms)"),
    Spec("tokens", "mean", log(1.25), "tokens per answer (log)"),
)
# Latency and tokens are tested as means of logs, but shown as geometric means in their units.
SHOWN_LABELS = {
    "latency": "latency of uncached attempts (geometric mean)",
    "tokens": "tokens per answer (geometric mean)",
}


def shown_label(spec: Spec) -> str:
    """The label for a table or chart that shows latency and tokens in their own units."""
    return SHOWN_LABELS.get(spec.name, spec.label)


@dataclass
class DayValues:
    """One run's per-question values. A rate metric's values are 1.0 for the bad outcome."""

    run_id: str
    values: dict[str, list[float]]
    fabricated: list[float]
    cache_hits: int  # attempts that hit the local call cache, which makes latency meaningless
    answer_words: list[int]
    source_named: list[int]  # 1 when an answered in-snapshot question cites its source page
    reported: dict[str, float | None]
    judge_errors: int = 0  # cited answers the judge could not judge: no verdict, counted apart
    cited_answers: int = 0  # answered, not abstained, with a citation: the ones the judge grades


def _share(xs: Sequence[float]) -> float | None:
    return round(fmean(xs), 4) if xs else None


def _positive(x: object) -> bool:
    """A usable latency: a finite number above zero (None and NaN are not)."""
    return isinstance(x, int | float) and isfinite(x) and x > 0


def _cited(output: Mapping[str, Any]) -> bool:
    """An answer the judge grades: one with at least one citation."""
    raw = output.get("citations")
    return isinstance(raw, list) and any(isinstance(c, dict) for c in raw)


def _judge_error(grades: Mapping[str, float]) -> bool:
    """An answer the judge could not judge: its only judge grade is judge_error."""
    return set(grades) == {JUDGE_ERROR}


def day_values(
    store: ResultsStore,
    run_id: str,
    cases: Mapping[str, Case],
    traffic: Mapping[str, TrafficQuestion],
    judge: str = "judge",
) -> DayValues:
    """The plan's per-question values for one online run, from its stored attempts and grades,
    judge_supported from the grades of one judge (by grader name, the validated judge unless
    asked). A judge error is no verdict: it is left out of judge_supported's values, the
    reported shares included, and counted."""
    outputs = {(a["case_id"], a["attempt"]): a["output"] for a in store.all_attempts(run_id)}
    keywords = store.grades_for(run_id, "answer_keywords")
    citations = store.grades_for(run_id, "citations")
    trajectory = store.grades_for(run_id, "trajectory")
    verdicts = store.grades_for(run_id, judge)
    v: dict[str, list[float]] = {s.name: [] for s in ALERT_FAMILY}
    fabricated: list[float] = []
    words: list[int] = []
    named: list[int] = []
    in_scope_supported: list[float] = []
    outside_answered: list[float] = []
    premise_declined: list[float] = []
    core: list[float] = []
    cache_hits = 0
    judge_errors = 0
    cited_answers = 0
    for u in store.attempt_usage(run_id):
        key = (str(u["case_id"]), int(u["attempt"]))
        case = cases.get(key[0])
        if case is None:
            continue
        failed = u["error"] is not None
        v["error"].append(float(failed))
        cache_hits += int(bool(u["cached_calls"]))
        if not u["cached_calls"] and _positive(u["latency_ms"]):
            v["latency"].append(log(float(u["latency_ms"])))
        calls = trajectory.get(key, {}).get("model_calls", float(u["model_calls"] or 0))
        if isfinite(calls):
            v["model_calls"].append(calls)
        judged = verdicts.get(key, {})
        errored = _judge_error(judged)  # no verdict, so not unsupported either: left out
        if case.type in IN_SCOPE and not errored:  # abstained, uncited, errored: unsupported
            in_scope_supported.append(float(judged.get("judge_supported") == 1.0))
        if failed:
            continue
        abstained = keywords.get(key, {}).get("abstained") == 1.0
        if case.type in IN_SCOPE:
            v["in_scope_abstention"].append(float(abstained))
        elif case.type == "unanswerable":
            outside_answered.append(float(not abstained))
        elif case.type == "false_premise":
            premise_declined.append(float(abstained))
        if abstained:
            continue
        output = outputs.get(key, {})
        cited_answers += _cited(output)
        cited = citations.get(key, {})
        if "uncited_answer" in cited:
            v["uncited_answer"].append(cited["uncited_answer"])
        if "fabricated_citation" in cited:
            fabricated.append(cited["fabricated_citation"])
        if "judge_supported" in judged:
            v["judge_supported"].append(1.0 - judged["judge_supported"])
            core.append(judged.get("judge_core_supported", 0.0))
        elif errored:
            judge_errors += 1
        tokens = int(u["input_tokens"] or 0) + int(u["output_tokens"] or 0)
        if tokens:
            v["tokens"].append(log(tokens))
        words.append(len(str(output.get("answer", "")).split()))
        q = traffic.get(key[0])
        if q is not None and q.source_url:
            raw = output.get("citations")
            urls = (
                {str(c.get("url")) for c in raw if isinstance(c, dict)}
                if isinstance(raw, list)
                else set()
            )
            named.append(int(q.source_url in urls))
    reported: dict[str, float | None] = {  # nothing judged is unknown, not 0 supported
        "judge_supported_over_all_in_scope": _share(in_scope_supported) if verdicts else None,
        "outside_snapshot_answered": _share(outside_answered),
        "false_premise_declined": _share(premise_declined),
        "judge_core_supported": _share(core),
        "median_answer_words": float(median(words)) if words else None,
        "source_named_share": _share(named),
    }
    return DayValues(
        run_id, v, fabricated, cache_hits, words, named, reported, judge_errors, cited_answers
    )


def _shift(day: float | None, ref: float | None) -> float | None:
    if day is None or not ref:
        return None
    return round(abs(day - ref) / ref, 4)


def check_day(
    day: DayValues,
    reference: Sequence[DayValues],
    *,
    judge_enabled: bool,
    seed: int = 0,
    judge_note: str | None = None,
) -> dict[str, Any]:
    """The full rule for one day against the pooled reference days. judge_note says why the
    judge is out of the family, when it is (judge_standing). The judge is untrusted on a day
    whose answers shifted style past TRUST_SHIFT, or whose judge errors pass JUDGE_ERROR_SHARE
    of its cited answers; the report counts the judge errors either way."""
    family = [s for s in ALERT_FAMILY if judge_enabled or s.name != "judge_supported"]
    rows: dict[str, dict[str, Any]] = {}
    pvalues: dict[str, float] = {}
    for spec in ALERT_FAMILY:
        d = day.values[spec.name]
        r = [x for ref in reference for x in ref.values[spec.name]]
        row: dict[str, Any] = {
            "metric": spec.name,
            "label": spec.label,
            "kind": spec.kind,
            "floor": spec.floor,
            "n_day": len(d),
            "n_ref": len(r),
            "day": fmean(d) if d else None,
            "ref": fmean(r) if r else None,
            "effect": fmean(d) - fmean(r) if d and r else None,
            "p": None,
            "detectable": None,
        }
        if spec.name == "latency" and day.cache_hits:
            row["status"] = f"refused: {day.cache_hits} attempts hit the local call cache"
        elif spec.name == "judge_supported" and not judge_enabled:
            row["status"] = judge_note or PENDING
        elif not d or not r:
            row["status"] = "no data"
        else:
            if spec.kind == "rate":
                row["p"] = fisher_greater(round(sum(d)), len(d), round(sum(r)), len(r))
            else:
                row["p"] = permutation_greater(d, r, seed=seed)
            pvalues[spec.name] = row["p"]
        rows[spec.name] = row
    for spec in ALERT_FAMILY:  # every metric's blind spot, whether tested today or not
        d = day.values[spec.name]
        r = [x for ref in reference for x in ref.values[spec.name]]
        if not d or not r:
            continue
        # Holm's first step, which a lone rise must pass: alpha over the metrics tested, one
        # more for a metric that would join them (a pending judge, a refused latency)
        level = ALPHA / (len(pvalues) + (spec.name not in pvalues))
        rows[spec.name]["detectable_alpha"] = level
        rows[spec.name]["detectable"] = (
            detectable_rate_rise(
                bad_ref=round(sum(r)), n_ref=len(r), n_day=len(d), alpha=level, floor=spec.floor
            )
            if spec.kind == "rate"
            else detectable_mean_rise(pstdev(r), len(d), len(r), alpha=level, floor=spec.floor)
        )
        if rows[spec.name]["detectable"] is None:  # the worst blind spot, not a missing number
            rows[spec.name]["detectable_note"] = CANNOT_ALERT
    rejected = holm(pvalues, alpha=ALPHA)
    for name in pvalues:
        row = rows[name]
        alert = rejected[name] and row["effect"] >= row["floor"] - 1e-12
        row["holm_rejected"] = rejected[name]
        row["status"] = "alert" if alert else "ok"

    rf = [x for ref in reference for x in ref.fabricated]
    fab: dict[str, Any] = {"n_day": len(day.fabricated), "n_ref": len(rf), "p": None}
    fab["day"] = fmean(day.fabricated) if day.fabricated else None
    fab["ref"] = fmean(rf) if rf else None
    fab["detectable"] = None
    if day.fabricated and rf:
        fab["p"] = fisher_greater(
            round(sum(day.fabricated)), len(day.fabricated), round(sum(rf)), len(rf)
        )
        fab["detectable"] = detectable_rate_rise(  # its own rule: p below 0.01, no floor
            bad_ref=round(sum(rf)),
            n_ref=len(rf),
            n_day=len(day.fabricated),
            alpha=FABRICATION_P,
            strict=True,
        )
        if fab["detectable"] is None:
            fab["detectable_note"] = CANNOT_ALERT
    fab["alert"] = fab["p"] is not None and fab["p"] < FABRICATION_P

    ref_words = [w for ref in reference for w in ref.answer_words]
    ref_named = [n for ref in reference for n in ref.source_named]
    shifts = {
        "median_answer_words": _shift(
            day.reported["median_answer_words"], median(ref_words) if ref_words else None
        ),
        "source_named_share": _shift(
            day.reported["source_named_share"], fmean(ref_named) if ref_named else None
        ),
    }
    errors, cited = day.judge_errors, day.cited_answers
    erring = bool(cited) and errors / cited > JUDGE_ERROR_SHARE  # the exact share, not rounded
    trusted = all(s is None or s <= TRUST_SHIFT for s in shifts.values()) and not erring
    if not trusted and rows["judge_supported"]["status"] == "ok":
        rows["judge_supported"]["status"] = "ok, but the judge is untrusted today: not a clear"
    alerts = [name for name, row in rows.items() if row["status"] == "alert"]
    if fab["alert"]:
        alerts.append("fabricated_citation")
    return {
        "run_id": day.run_id,
        "reference_runs": [r.run_id for r in reference],
        "family_size": len(pvalues),  # what Holm ran over: a metric with no test leaves it
        "metrics": list(rows.values()),
        "fabricated_citation": fab,
        "judge": {
            "in_family": judge_enabled,
            "standing": "in the family" if judge_enabled else (judge_note or PENDING),
            "trusted": trusted,
            "shifts": shifts,
            "errors": errors,  # judge_error grades: no verdict, so out of judge_supported
            "cited": cited,
            "error_share": round(errors / cited, 4) if cited else None,
        },
        "reported": day.reported,
        "alerts": alerts,
        # family metrics with no test today (no data, or latency refused): a quiet day with
        # any of these is not a clear
        "untested": [s.name for s in family if rows[s.name]["p"] is None],
    }


def not_cleared(report: Mapping[str, Any]) -> list[str]:
    """Why a day without an alert still does not clear: family metrics left untested, and a
    judge in the family that is untrusted today. Empty means the day is clear."""
    reasons = [
        f"{row['metric']} ({row['status']})"
        for row in report["metrics"]
        if row["metric"] in report["untested"]
    ]
    if report["judge"]["in_family"] and not report["judge"]["trusted"]:
        reasons.append("judge_supported (the judge is untrusted today)")
    return reasons


def verdict_code(report: Mapping[str, Any]) -> int:
    """The monitor's exit code for a day's report: 1 on an alert, 3 when there is none but the
    day is not cleared, 0 when it is clear. A recorded day is answered with its record's code."""
    if report["alerts"]:
        return 1
    return 3 if not_cleared(report) else 0


def leave_one_day_out(days: Sequence[DayValues], *, judge_enabled: bool) -> list[dict[str, Any]]:
    """Each reference day against the other four, with the full rule. Any alert, or a family
    metric left untested, means the reference is too noisy (or too thin) to judge later days
    by, and the demo stops."""
    return [
        check_day(d, [o for o in days if o is not d], judge_enabled=judge_enabled) for d in days
    ]


def noisy_days(days: Mapping[int, str], checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The leave-one-day-out reports that stop a freeze: day -> its alerts and untested metrics."""
    out: dict[str, Any] = {}
    for d, r in zip(sorted(days), checks, strict=True):
        if r["alerts"] or r["untested"]:
            out[str(d)] = {"alerts": r["alerts"], "untested": r["untested"]}
    return out


def judge_standing(
    store: ResultsStore, reference_runs: Iterable[str], grader: str = "judge"
) -> tuple[bool, str]:
    """Whether a judge's judge_supported (by grader name, the validated judge unless asked) is
    in the alert family, and why or why not: the judge passed the author's blind labels in its
    own judge check on these reference runs (for the validated judge, the 30 with at most 2
    false passes), and the leave-one-day-out check with it in the family raised no alert. Read
    from the results database, never from judge-check.json, which is only its published copy. A
    check on another reference or with anyone else's labels does not count, nor does one whose
    sampled answers were re-judged since. Another judge's check never counts for this one, and
    a pin (online.ONLINE_JUDGE_PINS) counts for nothing here."""
    runs = list(reference_runs)
    validated = grader == "judge"
    name = "the judge" if validated else grader
    labels = "the 30 blind labels" if validated else "its blind labels"
    kept = recorded_judge_check(store, runs, grader)
    if kept is None:
        return False, PENDING if validated else f"pending: {name} has not yet passed {labels}"
    if kept.get("labeler") != JUDGE_CHECK_LABELER:
        return False, "out of the family: the recorded judge check is not the author's"
    if not kept.get("passed"):
        if kept.get("labels_passed"):
            return False, f"out of the family: the reference is too noisy with {name} in it"
        return False, f"out of the family: {name} failed {labels}"
    drawn = recorded_sample(store, runs, grader)
    if drawn is None or verdict_digests(store, drawn["runs"], grader) != drawn.get(
        "verdict_digests"
    ):
        return False, "out of the family: the sampled answers were re-judged after its check"
    return True, f"in the family: {name} passed {labels}"


def judge_enabled(
    store: ResultsStore, reference_runs: Iterable[str], grader: str = "judge"
) -> bool:
    """Whether a judge's judge_supported is in the alert family (judge_standing, without the
    reason)."""
    return judge_standing(store, reference_runs, grader)[0]


def foreign_record(store: ResultsStore, reference_runs: Iterable[str], key: str) -> str | None:
    """A reference run that already carries this judge-check record (one judge's sample or
    result: sample_record, check_record) made for another set of runs, or None. Records are
    written once: a run belongs to one judge check per judge, so no check on another (or
    hand-edited) reference can overwrite the real one's record, while another judge's check on
    the same runs keeps its own records beside it."""
    runs = sorted(reference_runs)
    for run in runs:
        try:
            kept = store.run_info(run)["manifest"].get(key)
        except KeyError:
            continue
        if kept and sorted(kept.get("reference_runs") or []) != runs:
            return run
    return None


def day_runs(store: ResultsStore, day: int, *, champion_only: bool = False) -> list[str]:
    """The day's complete online runs that are not shadows, oldest first."""
    out = []
    for run in store.online_runs():
        role = run["manifest"].get("online") or {}
        if role.get("day") != day or run["status"] != "complete" or role.get("shadow"):
            continue
        if not champion_only or role.get("champion"):
            out.append(str(run["run_id"]))
    return out


def day_run(store: ResultsStore, day: int, *, champion_only: bool = False) -> str | None:
    """The day's one complete online run that is not a shadow. With two or more, which one
    counts is a decision to make, never a default, so it is refused."""
    runs = day_runs(store, day, champion_only=champion_only)
    if len(runs) > 1:
        raise ValueError(f"day {day} has {len(runs)} complete online runs: {', '.join(runs)}")
    return runs[0] if runs else None


def _stored_path(raw: object) -> Path:
    """A path recorded in a manifest, which may have been written on Windows."""
    return Path(str(raw).replace("\\", "/"))


def load_day(
    store: ResultsStore, run_id: str, traffic_dir: Path, judge: str = "judge"
) -> DayValues:
    """An online run's values, judge_supported from one judge's grades (by grader name, the
    validated judge unless asked). Refused unless the run is complete, has attempts, was judged
    in full by that judge at its pin (online.ONLINE_JUDGE_PINS), as its judge note says, and its
    cases and traffic files are the ones it ran: a gap would otherwise read as a quiet day. A
    cited answer is judged when it has a verdict, or when its only judge grade is judge_error:
    the judge could not judge it, which is counted, never read as a verdict."""
    pin = judge_pin(judge)
    if pin is None:
        raise ValueError(
            f"{judge} has no online pin, so its grades on {run_id} are not read: only a pinned "
            "judge grades online runs"
        )
    info = store.run_info(run_id)
    if not info["online"]:
        raise ValueError(f"{run_id} is not an online run")
    if info["status"] != "complete":
        raise ValueError(f"{run_id} is {info['status']}, not complete")
    role = info["manifest"].get("online") or {}
    day = role.get("day")
    if day is None:
        raise ValueError(f"{run_id} records no day; run it with `evals online-run DAY`")
    suite = info["manifest"].get("suite", {})
    path = _stored_path(suite.get("path", ""))
    if not path.is_file() or file_sha256(path) != suite.get("sha256"):
        raise ValueError(f"{run_id}: its cases file {path} is missing or has changed since it ran")
    traffic_path = traffic_dir / f"day-{int(day):02d}.jsonl"
    if not traffic_path.is_file() or file_sha256(traffic_path) != role.get("traffic_sha256"):
        raise ValueError(
            f"{run_id}: its traffic file {traffic_path} is missing or not the one it ran"
        )
    note = info["manifest"].get(judge)
    version = note.get("version") if isinstance(note, dict) else None
    if version != pin:
        named = "" if judge == "judge" else f" ({judge})"
        raise ValueError(
            f"{run_id} is not judged by the pinned judge {pin}{named}: uv run evals judge {run_id}"
        )
    grades = store.grades_for(run_id, judge)
    cited = [
        (a["case_id"], grades.get((a["case_id"], a["attempt"]), {}))
        for a in store.answered_attempts(run_id)
        if _cited(a["output"])
    ]
    unjudged = [case for case, g in cited if "judge_supported" not in g and not _judge_error(g)]
    if unjudged:
        raise ValueError(
            f"{run_id}: {len(unjudged)} cited answer(s) have no judge verdict (first "
            f"{unjudged[0]}); judge them before checking the day"
        )
    if both := [case for case, g in cited if "judge_supported" in g and JUDGE_ERROR in g]:
        raise ValueError(
            f"{run_id}: {len(both)} answer(s) hold both a judge verdict and a judge error (first "
            f"{both[0]}); judge the run again with --replace"
        )
    traffic = {q.id: q for q in load_traffic(traffic_path)}
    cases = {c.id: c for c in load_suite(path)}
    values = day_values(store, run_id, cases, traffic, judge)
    if not values.values["error"]:
        raise ValueError(f"{run_id} has no attempts")
    return values


def archive_reference_files(stamp: str, *, keep_judge_check: bool = False) -> list[Path]:
    """Move everything made against the current reference into results/online/archive/STAMP/,
    so no alert, post-hoc reading or judge check made against the old reference is read against
    a new one. With keep_judge_check (a re-record of the same runs), the judge check's sample and
    result stay: they belong to the runs, not to the code, and moving them would reopen a final
    score."""
    moved = []
    check = REFERENCE_PATH.with_name("reference-check.json")
    paths = [REFERENCE_PATH, check, ALERTS_PATH, POST_HOC_PATH]
    if not keep_judge_check:
        paths += [JUDGE_SAMPLE_PATH, JUDGE_CHECK_PATH]
    for path in paths:
        if path.exists():
            dest = REFERENCE_PATH.parent / "archive" / stamp / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.replace(dest)
            moved.append(dest)
    return moved


def recorded_judge_check(
    store: ResultsStore, reference_runs: Iterable[str], grader: str = "judge"
) -> dict[str, Any] | None:
    """A judge's first complete judge-check result for these reference runs (the validated
    judge's unless another grader is named), as kept in the results database (every reference
    run carries it once scored), or None. The database is the record; judge-check.json is only
    its published copy, so no file written, edited or deleted by hand can make, change or reopen
    a final score."""
    kept = _kept(store, reference_runs, check_record(grader))
    return kept if kept is not None and kept.get("complete") else None


def recorded_sample(
    store: ResultsStore, reference_runs: Iterable[str], grader: str = "judge"
) -> dict[str, Any] | None:
    """A judge's check sample drawn for these reference runs (the validated judge's unless
    another grader is named), as kept in the results database (every reference run carries it
    from the draw): drawn_at, reference_runs, runs (run -> case ids) and verdict_digests.
    judge-check-sample.json is only its published copy."""
    return _kept(store, reference_runs, sample_record(grader))


def sampled_cases(store: ResultsStore, reference_runs: Iterable[str]) -> dict[str, set[str]]:
    """Run -> case ids of every judge's check sample drawn for these reference runs: answers
    labeled blind, which the dashboard never shows with a verdict."""
    runs = sorted(reference_runs)
    keys: set[str] = set()
    for run in runs:
        try:
            keys.update(k for k in store.run_info(run)["manifest"] if _is_sample_key(k))
        except KeyError:  # a run the database does not know holds no record
            continue
    out: dict[str, set[str]] = {}
    for key in sorted(keys):
        for run, cases in ((_kept(store, runs, key) or {}).get("runs") or {}).items():
            out.setdefault(str(run), set()).update(cases)
    return out


def _kept(store: ResultsStore, reference_runs: Iterable[str], key: str) -> dict[str, Any] | None:
    runs = sorted(reference_runs)
    for run in runs:
        try:
            kept = store.run_info(run)["manifest"].get(key)
        except KeyError:  # a run the database does not know holds no record
            continue
        if kept and sorted(kept.get("reference_runs") or []) == runs:
            return dict(kept)
    return None


def pending_blind(store: ResultsStore) -> set[str]:
    """Every case id of a drawn judge-check sample, of any judge, whose check awaits its first
    complete score, read from the results database. Online case ids name their day
    (online-dNN-...), and a free cache replay of a reference day answers and judges them the
    same, so they are held back in any run that holds them, not only in the reference runs. One
    judge's scored check releases its own sample only."""
    out: set[str] = set()
    for run in store.online_runs():
        manifest = run["manifest"]
        for key in [k for k in manifest if _is_sample_key(k)]:
            drawn = manifest[key]
            if not drawn:
                continue
            scored = manifest.get(_check_key_of(key))
            done = sorted((scored or {}).get("reference_runs") or []) == sorted(
                drawn.get("reference_runs") or []
            )
            if scored and scored.get("complete") and done:
                continue
            for cases in (drawn.get("runs") or {}).values():
                out.update(cases)
    return out


def blind_cases(store: ResultsStore, run_id: str) -> set[str]:
    """The pending judge checks' sampled answers that this run holds (pending_blind), whichever
    judge drew them. Their judge verdicts and reasons, every judge's, must not reach the labeler
    before that check's first complete score: `label --show-judge`, `judge-validate`,
    `judge-probe`, `compare` on a judge metric and `trace` hold them back. Empty once every
    check is scored, or for a run holding none."""
    pending = pending_blind(store)
    if not pending:
        return set()
    return pending & {str(a["case_id"]) for a in store.all_attempts(run_id)}


def _verdicts(store: ResultsStore, run: str) -> dict[str, bool]:
    """case id -> did the judge find the answer fully supported, for the run's judged answers."""
    return {
        case: grades["judge_supported"] == 1.0
        for (case, attempt), grades in store.grades_for(run, "judge").items()
        if attempt == 1 and "judge_supported" in grades
    }


def judge_check_sample(
    store: ResultsStore,
    runs: Mapping[int, str],
    shown: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    """Six judged answers per reference day, three the judge passed and three it failed, drawn
    with a seed per day, as case ids per run, sorted so their order shows no verdict. A plain
    random draw would hold only about 7 or 8 judge passes in 30 (10 in the draw of 2026-10-19),
    since the judge passes about a quarter of online answers, and a false pass can happen only
    on those (decision 6). Answers already shown with the judge's verdict (`shown`, run -> case
    ids: the dashboard's least-supported lists) are left out, or the labeler could know them.
    Drawn once and saved, with its verdict digests, before any label exists. Refused unless
    every reference day has three of each."""
    if sorted(runs) != list(REFERENCE_DAYS):
        raise ValueError(f"the judge check samples the reference days {list(REFERENCE_DAYS)}")
    need_passed = JUDGE_CHECK_PASSED_PER_DAY
    need_failed = JUDGE_CHECK_PER_DAY - JUDGE_CHECK_PASSED_PER_DAY
    out: dict[str, list[str]] = {}
    for day, run in sorted(runs.items()):
        seen = set((shown or {}).get(run, ()))
        verdicts = {case: ok for case, ok in _verdicts(store, run).items() if case not in seen}
        passed = sorted(case for case, ok in verdicts.items() if ok)
        failed = sorted(case for case, ok in verdicts.items() if not ok)
        if len(passed) < need_passed or len(failed) < need_failed:
            raise ValueError(
                f"{run} has {len(passed)} judge-passed and {len(failed)} judge-failed answers; "
                f"the check needs {need_passed} and {need_failed}"
            )
        rng = random.Random(f"judge-check-{day}")
        out[run] = sorted(rng.sample(passed, need_passed) + rng.sample(failed, need_failed))
    return out


def verdict_digests(
    store: ResultsStore, sample: Mapping[str, Sequence[str]], grader: str = "judge"
) -> dict[str, str]:
    """Per run, a digest of one judge's grades on the sampled answers (the validated judge's
    unless another grader is named), values and reasons both. Saved with the sample when it is
    drawn and compared before labeling and scoring, so any re-judging after the draw is caught,
    even a swap of one pass and one fail that keeps a day at 3 and 3. The reasons, which the
    labeler never sees, keep the digest from being worked back to the verdicts: a day's 6
    answers have only 20 possible 3-and-3 splits."""
    out: dict[str, str] = {}
    for run, cases in sample.items():
        grades = store.grade_reasons(run, grader)
        rows = [
            [case, sorted([m, v, why] for m, (v, why) in grades.get((case, 1), {}).items())]
            for case in sorted(cases)
        ]
        out[run] = sha256_text(json.dumps(rows))
    return out


def judge_check_score(
    store: ResultsStore, sample: Mapping[str, Sequence[str]], labeler: str
) -> dict[str, Any]:
    """The judge's errors on the sample. A false pass: it found an answer fully supported and
    the labeler did not. The labels pass only if all 30 are in and false passes, out of the 15
    answers the judge passed, are at most JUDGE_CHECK_MAX_FALSE_PASSES. False fails (it failed
    an answer the labeler passed) are counted too and reported, without a bar: they inflate
    "not supported", the alert direction. A sampled answer without a judge verdict is refused,
    not read as a judge fail, which could never be a false pass; so is a day whose verdicts no
    longer split 3 and 3, since the judge was then re-run after the draw."""
    if len(sample) != len(REFERENCE_DAYS) or any(
        len(cases) != JUDGE_CHECK_PER_DAY for cases in sample.values()
    ):
        raise ValueError(
            f"the sample must hold {JUDGE_CHECK_PER_DAY} answers from each of the "
            f"{len(REFERENCE_DAYS)} reference runs"
        )
    rows: list[dict[str, Any]] = []
    for run, cases in sample.items():
        labels = {x["case_id"]: x for x in store.labels(run, labeler) if x["attempt"] == 1}
        judge = store.grades_for(run, "judge")
        passes = 0
        for case in cases:
            verdict = judge.get((case, 1), {}).get("judge_supported")
            if verdict is None:
                raise ValueError(f"{run} {case}: the sampled answer has no judge verdict")
            passes += verdict == 1.0
            label = labels.get(case)
            rows.append(
                {
                    "run": run,
                    "case": case,
                    "judge_supported": verdict == 1.0,
                    "labeled_supported": None if label is None else label["supported"],
                }
            )
        if passes != JUDGE_CHECK_PASSED_PER_DAY:
            raise ValueError(
                f"{run}: {passes} of its sampled answers are judge passes, not "
                f"{JUDGE_CHECK_PASSED_PER_DAY}; its verdicts changed after the sample was drawn"
            )
    labeled = [r for r in rows if r["labeled_supported"] is not None]
    false_passes = [r for r in labeled if r["judge_supported"] and not r["labeled_supported"]]
    false_fails = [r for r in labeled if not r["judge_supported"] and r["labeled_supported"]]
    complete = len(labeled) == len(rows)
    return {
        "labeler": labeler,
        "reference_runs": sorted(sample),
        "answers": len(rows),
        "judge_passed": sum(r["judge_supported"] for r in rows),
        "labeled": len(labeled),
        "complete": complete,
        "false_passes": len(false_passes),
        "max_false_passes": JUDGE_CHECK_MAX_FALSE_PASSES,
        "false_fails": len(false_fails),
        "labels_passed": complete and len(false_passes) <= JUDGE_CHECK_MAX_FALSE_PASSES,
        "rows": rows,
    }
