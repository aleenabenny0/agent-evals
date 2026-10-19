"""The online monitor (phase 7 part 3, docs/plans/phase-7-online-evals.md): is a day worse than
the frozen reference days?

A day's online run becomes per-question values for the plan's alert family. Each metric is
tested one-sided in its bad direction against the pooled reference days (stats/monitor.py), the
family is Holm-corrected, and an alert also needs the effect to reach the metric's practical
floor. Fabricated citations have their own rule outside Holm, because at this volume Holm would
fire only at several times the baseline. Some numbers are only reported. judge_supported joins
the family only once Aleena's 30 blind labels have passed, and a day whose answers changed style
marks the judge untrusted: an alert still stands, but a quiet judge_supported does not clear it.
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

from agent_evals.harness.manifest import file_sha256
from agent_evals.online import ONLINE_JUDGE_VERSION
from agent_evals.simulate import TrafficQuestion, load_traffic
from agent_evals.stats.monitor import (
    detectable_mean_rise,
    detectable_rate_rise,
    fisher_greater,
    holm,
    permutation_greater,
)
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import Case

ALPHA = 0.05
FABRICATION_P = 0.01  # the separate safety rule: one-sided Fisher, no floor
TRUST_SHIFT = 0.25  # a style shift past this marks the day's judge_supported untrusted
REFERENCE_DAYS = (1, 2, 3, 4, 5)
REFERENCE_PATH = Path("results/online/reference.json")
ALERTS_PATH = Path("results/online/alerts.json")
JUDGE_CHECK_PATH = Path("results/online/judge-check.json")
JUDGE_SAMPLE_PATH = Path("results/online/judge-check-sample.json")
JUDGE_CHECK_PER_DAY = 6  # 30 answers over the five reference days
JUDGE_CHECK_MAX_FALSE_PASSES = 2  # the bar of iteration 4's spot check
IN_SCOPE = ("lookup", "multi_step")  # lookups and comparisons: declining one is a failure


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


def _share(xs: Sequence[float]) -> float | None:
    return round(fmean(xs), 4) if xs else None


def _positive(x: object) -> bool:
    """A usable latency: a finite number above zero (None and NaN are not)."""
    return isinstance(x, int | float) and isfinite(x) and x > 0


def day_values(
    store: ResultsStore,
    run_id: str,
    cases: Mapping[str, Case],
    traffic: Mapping[str, TrafficQuestion],
) -> DayValues:
    """The plan's per-question values for one online run, from its stored attempts and grades."""
    outputs = {(a["case_id"], a["attempt"]): a["output"] for a in store.all_attempts(run_id)}
    keywords = store.grades_for(run_id, "answer_keywords")
    citations = store.grades_for(run_id, "citations")
    trajectory = store.grades_for(run_id, "trajectory")
    judge = store.grades_for(run_id, "judge")
    v: dict[str, list[float]] = {s.name: [] for s in ALERT_FAMILY}
    fabricated: list[float] = []
    words: list[int] = []
    named: list[int] = []
    in_scope_supported: list[float] = []
    outside_answered: list[float] = []
    premise_declined: list[float] = []
    core: list[float] = []
    cache_hits = 0
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
        judged = judge.get(key, {})
        if case.type != "unanswerable":  # abstained, uncited and errored count as unsupported
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
        cited = citations.get(key, {})
        if "uncited_answer" in cited:
            v["uncited_answer"].append(cited["uncited_answer"])
        if "fabricated_citation" in cited:
            fabricated.append(cited["fabricated_citation"])
        if "judge_supported" in judged:
            v["judge_supported"].append(1.0 - judged["judge_supported"])
            core.append(judged.get("judge_core_supported", 0.0))
        tokens = int(u["input_tokens"] or 0) + int(u["output_tokens"] or 0)
        if tokens:
            v["tokens"].append(log(tokens))
        output = outputs.get(key, {})
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
        "judge_supported_over_all_in_scope": _share(in_scope_supported) if judge else None,
        "outside_snapshot_answered": _share(outside_answered),
        "false_premise_declined": _share(premise_declined),
        "judge_core_supported": _share(core),
        "median_answer_words": float(median(words)) if words else None,
        "source_named_share": _share(named),
    }
    return DayValues(run_id, v, fabricated, cache_hits, words, named, reported)


def _shift(day: float | None, ref: float | None) -> float | None:
    if day is None or not ref:
        return None
    return round(abs(day - ref) / ref, 4)


def check_day(
    day: DayValues, reference: Sequence[DayValues], *, judge_enabled: bool, seed: int = 0
) -> dict[str, Any]:
    """The full rule for one day against the pooled reference days."""
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
            row["status"] = "pending: the judge has not yet passed Aleena's 30 blind labels"
        elif not d or not r:
            row["status"] = "no data"
        else:
            if spec.kind == "rate":
                row["p"] = fisher_greater(int(sum(d)), len(d), int(sum(r)), len(r))
                row["detectable"] = detectable_rate_rise(
                    fmean(r), len(d), len(r), alpha=ALPHA / len(family)
                )
            else:
                row["p"] = permutation_greater(d, r, seed=seed)
                row["detectable"] = detectable_mean_rise(
                    pstdev(r), len(d), len(r), alpha=ALPHA / len(family)
                )
            pvalues[spec.name] = row["p"]
        rows[spec.name] = row
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
    if day.fabricated and rf:
        fab["p"] = fisher_greater(
            int(sum(day.fabricated)), len(day.fabricated), int(sum(rf)), len(rf)
        )
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
    trusted = all(s is None or s <= TRUST_SHIFT for s in shifts.values())
    if not trusted and rows["judge_supported"]["status"] == "ok":
        rows["judge_supported"]["status"] = "ok, but the judge is untrusted today: not a clear"
    alerts = [name for name, row in rows.items() if row["status"] == "alert"]
    if fab["alert"]:
        alerts.append("fabricated_citation")
    return {
        "run_id": day.run_id,
        "reference_runs": [r.run_id for r in reference],
        "family_size": len(family),
        "metrics": list(rows.values()),
        "fabricated_citation": fab,
        "judge": {"in_family": judge_enabled, "trusted": trusted, "shifts": shifts},
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


def judge_enabled(path: Path, reference_runs: Iterable[str]) -> bool:
    """Whether judge_supported is in the alert family: the judge passed Aleena's 30 blind labels
    (at most 2 false passes) on these reference runs, and the leave-one-day-out check with it in
    the family raised no alert. A check made on another reference does not count."""
    if not path.exists():
        return False
    result = json.loads(path.read_text(encoding="utf-8"))
    same = sorted(result.get("reference_runs") or []) == sorted(reference_runs)
    return bool(result.get("passed")) and same


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


def load_day(store: ResultsStore, run_id: str, traffic_dir: Path) -> DayValues:
    """An online run's values. Refused unless the run is complete, has attempts, was judged in
    full by the pinned judge, and its cases and traffic files are the ones it ran: a gap would
    otherwise read as a quiet day."""
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
    version = (info["manifest"].get("judge") or {}).get("version")
    if version != ONLINE_JUDGE_VERSION:
        raise ValueError(
            f"{run_id} is not judged by the pinned judge {ONLINE_JUDGE_VERSION}: "
            f"uv run evals judge {run_id}"
        )
    judge = store.grades_for(run_id, "judge")
    unjudged = [
        a["case_id"]
        for a in store.answered_attempts(run_id)
        if isinstance(a["output"].get("citations"), list)
        and any(isinstance(c, dict) for c in a["output"]["citations"])
        and "judge_supported" not in judge.get((a["case_id"], a["attempt"]), {})
    ]
    if unjudged:
        raise ValueError(
            f"{run_id}: {len(unjudged)} cited answer(s) have no judge verdict (first "
            f"{unjudged[0]}); judge them before checking the day"
        )
    traffic = {q.id: q for q in load_traffic(traffic_path)}
    cases = {c.id: c for c in load_suite(path)}
    values = day_values(store, run_id, cases, traffic)
    if not values.values["error"]:
        raise ValueError(f"{run_id} has no attempts")
    return values


def archive_reference_files(stamp: str) -> list[Path]:
    """Move everything made against the current reference into results/online/archive/STAMP/,
    so no alert or judge check made against the old reference is read against a new one."""
    moved = []
    check = REFERENCE_PATH.with_name("reference-check.json")
    for path in (REFERENCE_PATH, check, ALERTS_PATH, JUDGE_SAMPLE_PATH, JUDGE_CHECK_PATH):
        if path.exists():
            dest = REFERENCE_PATH.parent / "archive" / stamp / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.replace(dest)
            moved.append(dest)
    return moved


def judge_check_sample(store: ResultsStore, runs: Mapping[int, str]) -> dict[str, list[str]]:
    """Six judged answers per reference day, drawn with a seed per day, as case ids per run.
    Drawn once and saved before any label exists. Refused unless every reference day has six."""
    if sorted(runs) != list(REFERENCE_DAYS):
        raise ValueError(f"the judge check samples the reference days {list(REFERENCE_DAYS)}")
    out: dict[str, list[str]] = {}
    for day, run in sorted(runs.items()):
        judged = sorted(
            case
            for (case, attempt), grades in store.grades_for(run, "judge").items()
            if attempt == 1 and "judge_supported" in grades
        )
        if len(judged) < JUDGE_CHECK_PER_DAY:
            raise ValueError(
                f"{run} has {len(judged)} judged answers; the check needs {JUDGE_CHECK_PER_DAY}"
            )
        rng = random.Random(f"judge-check-{day}")
        out[run] = sorted(rng.sample(judged, JUDGE_CHECK_PER_DAY))
    return out


def judge_check_score(
    store: ResultsStore, sample: Mapping[str, Sequence[str]], labeler: str
) -> dict[str, Any]:
    """The judge's false passes on the sample: it found an answer fully supported and the labeler
    did not. The labels pass only if all 30 are in and false passes are at most
    JUDGE_CHECK_MAX_FALSE_PASSES. A sampled answer without a judge verdict is refused, not read
    as a judge fail, which could never be a false pass."""
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
        for case in cases:
            verdict = judge.get((case, 1), {}).get("judge_supported")
            if verdict is None:
                raise ValueError(f"{run} {case}: the sampled answer has no judge verdict")
            label = labels.get(case)
            rows.append(
                {
                    "run": run,
                    "case": case,
                    "judge_supported": verdict == 1.0,
                    "labeled_supported": None if label is None else label["supported"],
                }
            )
    labeled = [r for r in rows if r["labeled_supported"] is not None]
    false_passes = [r for r in labeled if r["judge_supported"] and not r["labeled_supported"]]
    complete = len(labeled) == len(rows)
    return {
        "labeler": labeler,
        "reference_runs": sorted(sample),
        "answers": len(rows),
        "labeled": len(labeled),
        "complete": complete,
        "false_passes": len(false_passes),
        "max_false_passes": JUDGE_CHECK_MAX_FALSE_PASSES,
        "labels_passed": complete and len(false_passes) <= JUDGE_CHECK_MAX_FALSE_PASSES,
        "rows": rows,
    }
