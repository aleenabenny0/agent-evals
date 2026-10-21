"""Predict, or read off, what the online monitor's family would do on a day run with one config
instead of another (phase 7 part 5, docs/plans/phase-7-online-evals.md).

Two runs on the same cases, a "deploy" and a "champion": for each of the six code-graded family
metrics and the fabricated-citation rule, both runs' values under the monitor's own per-attempt
definitions, the deploy-minus-champion effect with a case-level bootstrap interval, the frozen
online reference, and the chance that a day of the given size alerts if the effect carries over
to the reference rate (exact for rates, the normal approximation for means). Before a day: the
two configs' dev runs predict it. After it: the served run against its shadow gives the paired
difference, which separates the agent from the traffic.

Free and read-only. Run from the project folder:

    uv run python scripts/predict_day.py deploy=RUN champion=RUN [--cases PATH] [--questions N]
"""

from __future__ import annotations

import argparse
import json
from math import exp, log, sqrt
from pathlib import Path
from statistics import NormalDist, fmean, pstdev

import numpy as np

from agent_evals import monitor as mon
from agent_evals.cli import TRAFFIC_DIR
from agent_evals.stats.monitor import rate_alert_power
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import Case

N = NormalDist()
BOOTSTRAP_DRAWS = 2000
REFERENCE_QUESTIONS = 500  # the five reference days, 100 questions each


def per_case(
    store: ResultsStore, run: str, cases: dict[str, Case]
) -> dict[str, dict[str, list[float]]]:
    """metric -> case id -> that case's attempt values, under the monitor's definitions
    (monitor.day_values), checked against day_values itself."""
    out: dict[str, dict[str, list[float]]] = {s.name: {} for s in mon.ALERT_FAMILY}
    out["fabricated_citation"] = {}
    usage = {(str(u["case_id"]), int(u["attempt"])): u for u in store.attempt_usage(run)}
    keywords = store.grades_for(run, "answer_keywords")
    citations = store.grades_for(run, "citations")
    trajectory = store.grades_for(run, "trajectory")
    for (case_id, attempt), u in sorted(usage.items()):
        case = cases.get(case_id)
        if case is None:
            continue
        key = (case_id, attempt)
        failed = u["error"] is not None
        out["error"].setdefault(case_id, []).append(float(failed))
        if not u["cached_calls"] and mon._positive(u["latency_ms"]):
            out["latency"].setdefault(case_id, []).append(log(float(u["latency_ms"])))
        calls = trajectory.get(key, {}).get("model_calls", float(u["model_calls"] or 0))
        out["model_calls"].setdefault(case_id, []).append(calls)
        if failed:
            continue
        abstained = keywords.get(key, {}).get("abstained") == 1.0
        if case.type in mon.IN_SCOPE:
            out["in_scope_abstention"].setdefault(case_id, []).append(float(abstained))
        if abstained:
            continue
        cited = citations.get(key, {})
        if "uncited_answer" in cited:
            out["uncited_answer"].setdefault(case_id, []).append(cited["uncited_answer"])
        if "fabricated_citation" in cited:
            out["fabricated_citation"].setdefault(case_id, []).append(cited["fabricated_citation"])
        tokens = int(u["input_tokens"] or 0) + int(u["output_tokens"] or 0)
        if tokens:
            out["tokens"].setdefault(case_id, []).append(log(tokens))
    day = mon.day_values(store, run, cases, {})
    for m, d in out.items():
        if m == "judge_supported":
            continue  # out of the family since the judge check: not rebuilt here
        mine = day.fabricated if m == "fabricated_citation" else day.values[m]
        assert sorted(x for xs in d.values() for x in xs) == sorted(mine), (run, m)
    return out


def effect(
    deploy: dict[str, list[float]], champion: dict[str, list[float]], seed: int
) -> tuple[float, float, float]:
    """Deploy minus champion over the shared cases, with a 95% case-bootstrap interval."""
    shared = sorted(set(deploy) & set(champion))
    if not shared:
        return float("nan"), float("nan"), float("nan")

    def diff(pick: list[str]) -> float:
        return fmean([x for c in pick for x in deploy[c]]) - fmean(
            [x for c in pick for x in champion[c]]
        )

    rng = np.random.default_rng(seed)
    draws = [
        diff(list(rng.choice(shared, size=len(shared), replace=True)))
        for _ in range(BOOTSTRAP_DRAWS)
    ]
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return diff(shared), float(lo), float(hi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("deploy", help="deploy=RUN_ID: the run of the config under test")
    parser.add_argument("champion", help="champion=RUN_ID: the run to compare it with")
    parser.add_argument("--cases", type=Path, default=Path("suites/docs/dev.jsonl"))
    parser.add_argument("--db", type=Path, default=Path("results/evals.duckdb"))
    parser.add_argument("--questions", type=int, default=200, help="the day's size")
    args = parser.parse_args()
    labels = {}
    for arg in (args.deploy, args.champion):
        label, _, run = arg.partition("=")
        labels[label or run] = run
    (deploy_label, deploy_run), (champ_label, champ_run) = labels.items()
    cases = {c.id: c for c in load_suite(args.cases)}
    reference = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
    ref_runs = [str(r) for r in reference["days"].values()]
    store = ResultsStore(args.db, read_only=True)
    try:
        deploy = per_case(store, deploy_run, cases)
        champion = per_case(store, champ_run, cases)
        ref_days = [mon.load_day(store, r, TRAFFIC_DIR) for r in ref_runs]
    finally:
        store.close()
    ref_vals = {s.name: [x for d in ref_days for x in d.values[s.name]] for s in mon.ALERT_FAMILY}
    ref_vals["fabricated_citation"] = [x for d in ref_days for x in d.fabricated]
    specs = {s.name: s for s in mon.ALERT_FAMILY}
    alpha = mon.ALPHA / len([s for s in mon.ALERT_FAMILY if s.name != "judge_supported"])
    print(
        f"{deploy_label} minus {champ_label} on {args.cases} (case bootstrap); a day of "
        f"{args.questions} questions against the frozen reference"
    )
    head = (
        "metric",
        champ_label,
        deploy_label,
        "effect (95% CI)",
        "reference",
        "floor",
        "n day",
        "power",
    )
    print(
        f"{head[0]:22s} {head[1]:>9s} {head[2]:>9s} {head[3]:>28s} {head[4]:>10s} "
        f"{head[5]:>7s} {head[6]:>6s} {head[7]:>6s}"
    )
    for m in (*specs, "fabricated_citation"):
        if m == "judge_supported":
            continue
        d, c = deploy[m], champion[m]
        df = [x for xs in d.values() for x in xs]
        cf = [x for xs in c.values() for x in xs]
        if not df or not cf:
            print(f"{m:22s} {'-':>9s} {'-':>9s} {'no values on one side':>28s}")
            continue
        e, lo, hi = effect(d, c, seed=1)
        r = ref_vals[m]
        n_day = round(args.questions * len(r) / REFERENCE_QUESTIONS)  # the share with a value
        is_rate = m == "fabricated_citation" or specs[m].kind == "rate"
        if is_rate:
            floor = 0.0 if m == "fabricated_citation" else specs[m].floor
            level = mon.FABRICATION_P if m == "fabricated_citation" else alpha
            bad_ref, n_ref = round(sum(r)), len(r)
            true_rate = min(1.0, max(0.0, bad_ref / n_ref + e))
            power = rate_alert_power(
                true_rate,
                bad_ref,
                n_ref,
                n_day,
                alpha=level,
                floor=floor,
                strict=m == "fabricated_citation",
            )
            fmt = lambda v: f"{v * 100:+.1f} pts"  # noqa: E731
            shown = (
                f"{fmean(cf) * 100:.1f}%",
                f"{fmean(df) * 100:.1f}%",
                f"{fmean(r) * 100:.1f}%",
                f"+{floor * 100:.0f} pts",
            )
        else:
            floor = specs[m].floor
            sd = pstdev(r)
            crit = max(N.inv_cdf(1 - alpha) * sd * sqrt(1 / n_day + 1 / len(r)), floor)
            power = N.cdf((e - crit) / (sd / sqrt(n_day))) if sd else 0.0
            if m in ("latency", "tokens"):
                fmt = lambda v: f"{(exp(v) - 1) * 100:+.0f}%"  # noqa: E731
                unit = (
                    (lambda v: f"{exp(v) / 1000:.1f}k")
                    if m == "tokens"
                    else (lambda v: f"{exp(v) / 1000:.1f} s")
                )
                shown = (
                    unit(fmean(cf)),
                    unit(fmean(df)),
                    unit(fmean(r)),
                    f"+{(exp(floor) - 1) * 100:.0f}%",
                )
            else:
                fmt = lambda v: f"{v:+.2f}"  # noqa: E731
                shown = (f"{fmean(cf):.2f}", f"{fmean(df):.2f}", f"{fmean(r):.2f}", f"+{floor:.2f}")
        ci = f"{fmt(e)} ({fmt(lo)} to {fmt(hi)})"
        print(
            f"{m:22s} {shown[0]:>9s} {shown[1]:>9s} {ci:>28s} {shown[2]:>10s} "
            f"{shown[3]:>7s} {n_day:>6d} {power:>6.2f}"
        )


if __name__ == "__main__":
    main()
