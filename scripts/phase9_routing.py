"""Phase 9 routing analysis, as pre-registered in docs/plans/phase-9-routing.md.

fit       Dev only. Builds the attempt-1 matrix of the three models, fits every router, picks one
          per target by the plan's rule, and writes results/routing/dev-fit.json. Run it, and
          commit its output, before the test runs.
evaluate  Test. Refuses without dev-fit.json, refits the frozen settings on all of dev, and writes
          results/routing/test-result.json.
spot      Prints each new model's 30-answer spot-check sample and its label command.
spot-score  Scores the labeled samples against the judge by the plan's rule.

Costs are compared at list price with no prompt cache, since Haiku's runs predate the cache. The
billed cost of each run is reported beside.

Usage: uv run python -I -X utf8 scripts/phase9_routing.py fit|evaluate|spot|spot-score
"""

import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.config import load_config
from agent_evals.contracts import contract_problems
from agent_evals.routing import (
    Cascade,
    NearestNeighbours,
    Outcome,
    Question,
    Result,
    SingleModel,
    Target,
    best_knn,
    choose,
    evaluate,
    fit_by_type,
    oracle,
)
from agent_evals.stats.core import bootstrap_ci, mcnemar_exact, paired_bootstrap_diff, wilson_ci
from agent_evals.stats.power import detectable_delta
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

MODELS = ("haiku", "sonnet", "opus")  # cheapest first
CONFIGS = {
    "haiku": "configs/default.yaml",
    "sonnet": "configs/model-sonnet.yaml",
    "opus": "configs/model-opus.yaml",
}
RUNS = {
    "dev": {
        "haiku": "docs-20261016-214710-02abb7",
        "sonnet": "docs-20261103-042430-1042ba",
        # A rerun of docs-20261103-042856-b3bc58, whose four connection errors it retried. Every
        # other call replayed from the call cache, so the answers are that run's.
        "opus": "docs-20261103-044411-eb598a",
    },
    "test": {
        "haiku": "docs-20261016-222256-04f0f4",
        "sonnet": "docs-20261103-050320-f0a393",
        "opus": "docs-20261103-050826-6eb2fc",
    },
}
SUITES = {"dev": Path("suites/docs/dev.jsonl"), "test": Path("suites/docs/test.jsonl")}
JUDGE = "judge"  # the validated judge's grader name
TARGETS: tuple[Target, ...] = ("good", "correct")
SPOT_SALT = "phase-9-spot-check:"
LABELER = "aleena"
OUT = Path("results/routing")
DEV_FIT = OUT / "dev-fit.json"
TEST_RESULT = OUT / "test-result.json"
TEST_N = 150
MARGIN = -0.05  # non-inferiority margin against always-best, in rate


def load(
    split: str,
) -> tuple[list[Question], dict[str, dict[str, Outcome]], dict[str, Any], dict[str, str]]:
    """Attempt 1 of each model on one split, what the matrix table reports, and the questions
    whose good-answer outcome is unknown for some model: an answer the judge failed to grade. Those
    leave the good-answer comparison for every model, so it stays paired. They keep `correct`."""
    cases = sorted(load_suite(SUITES[split]), key=lambda c: c.id)
    questions = [Question(c.id, c.question, c.type) for c in cases]
    matrix: dict[str, dict[str, Outcome]] = {c.id: {} for c in cases}
    extra: dict[str, Any] = {}
    unjudged: dict[str, str] = {}
    store = ResultsStore(Path("results/evals.duckdb"))
    try:
        for model in MODELS:
            run_id = RUNS[split][model]
            info = store.run_info(run_id)
            if Path(info["manifest"]["suite"]["path"]) != SUITES[split]:
                raise SystemExit(f"{run_id} is not a run on {SUITES[split]}")
            pricing = load_config(Path(CONFIGS[model])).docs_agent.model.pricing
            assert pricing.input_per_mtok is not None and pricing.output_per_mtok is not None
            if (
                info["manifest"]["config"]["docs_agent"]["model"]["name"]
                != load_config(Path(CONFIGS[model])).docs_agent.model.name
            ):
                raise SystemExit(f"{run_id} did not run on {model}'s configured model")
            attempts = {a["case_id"]: a for a in store.all_attempts(run_id) if a["attempt"] == 1}
            usage = {u["case_id"]: u for u in store.attempt_usage(run_id) if u["attempt"] == 1}
            billed = {
                cid: cost
                for cid, cost in store.con.execute(
                    "SELECT case_id, cost_usd FROM attempts WHERE run_id = ? AND attempt = 1",
                    [run_id],
                ).fetchall()
            }
            code = store.grades_for(run_id, "answer_keywords")
            judged = store.grades_for(run_id, JUDGE)
            # A replayed call takes no time, so latency comes from attempts answered live, in any
            # run of this agent version on this split.
            latency = [
                float(ms)
                for (ms,) in store.con.execute(
                    "SELECT a.latency_ms FROM attempts a JOIN runs r USING (run_id) "
                    "WHERE r.agent_version = ? AND r.suite = ? AND NOT COALESCE(r.online, FALSE) "
                    "AND a.attempt = 1 AND a.cached_calls = 0 AND a.error IS NULL",
                    [info["agent_version"], info["suite"]],
                ).fetchall()
            ]
            for q in questions:
                a = attempts.get(q.case_id)
                if a is None:
                    raise SystemExit(f"{run_id} has no attempt 1 for {q.case_id}")
                out = a["output"] or {}
                abstained = bool(out.get("abstained", False))
                correct = code[(q.case_id, 1)]["correct"] == 1.0
                answered = not a["error"] and not abstained
                supported = judged.get((q.case_id, 1), {}).get("judge_supported")
                if answered and supported is None:
                    if judged.get((q.case_id, 1), {}).get("grader_error") != 1.0:
                        raise SystemExit(f"{run_id}: {q.case_id} #1 is answered but not judged")
                    unjudged[q.case_id] = f"{model}: the judge returned no verdict ({run_id})"
                escalate = bool(
                    a["error"]
                    or abstained
                    or contract_problems("docs", out)
                    or not out.get("citations")
                )
                u = usage[q.case_id]
                list_cost = (
                    u["input_tokens"] * pricing.input_per_mtok
                    + u["output_tokens"] * pricing.output_per_mtok
                ) / 1_000_000
                matrix[q.case_id][model] = Outcome(
                    correct=correct,
                    good=correct and (abstained or supported == 1.0),
                    cost_usd=list_cost,
                    escalate=escalate,
                )
            extra[model] = {
                "run": run_id,
                "list_usd": round(sum(matrix[q.case_id][model].cost_usd for q in questions), 4),
                # As one live run would be billed, prompt cache included where the run used it.
                "with_prompt_cache_usd": round(
                    sum(float(billed[q.case_id] or 0.0) for q in questions), 4
                ),
                "latency_ms_p50": round(statistics.median(latency)),
                "latency_ms_p95": round(statistics.quantiles(latency, n=20)[-1]),
                "latency_n": len(latency),
                "errors": sum(1 for q in questions if attempts[q.case_id]["error"]),
                "abstained": sum(
                    1 for q in questions if (attempts[q.case_id]["output"] or {}).get("abstained")
                ),
                "answered_supported": _share(
                    [
                        judged[(q.case_id, 1)]["judge_supported"] == 1.0
                        for q in questions
                        if (q.case_id, 1) in judged and "judge_supported" in judged[(q.case_id, 1)]
                    ]
                ),
            }
    finally:
        store.close()
    return questions, matrix, extra, unjudged


def scoped(questions: list[Question], target: Target, unjudged: dict[str, str]) -> list[Question]:
    """The questions a target is compared on: all of them for `correct`, and for good answers
    those whose outcome is known for every model."""
    return [q for q in questions if target == "correct" or q.case_id not in unjudged]


def _share(flags: list[bool]) -> dict[str, Any] | None:
    if not flags:
        return None
    e = wilson_ci(sum(flags), len(flags))
    return {"rate": round(e.mean, 4), "low": round(e.low, 4), "high": round(e.high, 4), "n": e.n}


def summary(r: Result) -> dict[str, Any]:
    rate = wilson_ci(sum(r.values), len(r.values))
    cost = bootstrap_ci(list(r.costs))
    return {
        "rate": round(rate.mean, 4),
        "rate_low": round(rate.low, 4),
        "rate_high": round(rate.high, 4),
        "cost_usd": round(cost.mean, 6),
        "cost_low": round(cost.low, 6),
        "cost_high": round(cost.high, 6),
        "n": len(r.values),
    }


def by_type_table(
    questions: list[Question], matrix: dict[str, dict[str, Outcome]], unjudged: dict[str, str]
) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for t in sorted({q.type for q in questions}):
        qs = [q for q in questions if q.type == t]
        table[t] = {
            m: {
                target: _share(
                    [matrix[q.case_id][m].value(target) for q in scoped(qs, target, unjudged)]
                )
                for target in TARGETS
            }
            for m in MODELS
        }
    return table


def fit() -> None:
    all_questions, matrix, extra, unjudged = load("dev")
    choices: dict[str, Any] = {}
    report: dict[str, Any] = {}
    for target in TARGETS:
        questions = scoped(all_questions, target, unjudged)
        single = {m: evaluate(SingleModel(m), questions, matrix, target) for m in MODELS}
        by_type = fit_by_type(questions, matrix, MODELS, target)
        cascade = evaluate(Cascade("haiku", "sonnet"), questions, matrix, target)
        k, tau, knn = best_knn(questions, matrix, MODELS, target)
        usable = [*single.values(), cascade, knn]
        choice = choose(single, usable, MODELS, target)
        chosen = next(r for r in usable if r.name == choice.chosen)
        best = single[choice.always_best]
        discordance = sum(a != b for a, b in zip(chosen.values, best.values, strict=True)) / len(
            questions
        )
        choices[target] = {
            "always_best": choice.always_best,
            "chosen": choice.chosen,
            "reason": choice.reason,
            "knn": {"k": k, "tau": tau},
            "by_type": dict(by_type.choice),
        }
        report[target] = {
            "single": {m: summary(r) for m, r in single.items()},
            "by_type_in_sample": summary(evaluate(by_type, questions, matrix, target)),
            "cascade": summary(cascade),
            "knn_leave_one_out": summary(knn),
            "oracle": summary(oracle(questions, matrix, MODELS, target)),
            "dev_discordance_chosen_vs_best": round(discordance, 4),
            "detectable_on_test": (
                round(detectable_delta(TEST_N, discordance), 4) if discordance > 0 else None
            ),
        }
    frozen = json.dumps(choices, sort_keys=True)
    OUT.mkdir(parents=True, exist_ok=True)
    DEV_FIT.write_text(
        json.dumps(
            {
                "plan": "docs/plans/phase-9-routing.md",
                "runs": RUNS["dev"],
                "choices": choices,
                "choices_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                "dev": report,
                "matrix_by_type": by_type_table(all_questions, matrix, unjudged),
                "models": extra,
                "unjudged": unjudged,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(choices, indent=1))
    print(f"wrote {DEV_FIT}")


def evaluate_test() -> None:
    if not DEV_FIT.exists():
        raise SystemExit(f"{DEV_FIT} is missing: fit on dev, and commit it, before reading test")
    fitted = json.loads(DEV_FIT.read_text(encoding="utf-8"))
    frozen = json.dumps(fitted["choices"], sort_keys=True)
    if hashlib.sha256(frozen.encode()).hexdigest() != fitted["choices_sha256"]:
        raise SystemExit("dev-fit.json's choices do not match their hash")
    dev_q, dev_m, _, dev_unjudged = load("dev")
    assert not dev_unjudged, "the frozen fit was made with every dev answer judged"
    all_questions, matrix, extra, unjudged = load("test")
    out: dict[str, Any] = {
        "runs": RUNS["test"],
        "choices_sha256": fitted["choices_sha256"],
        "unjudged": unjudged,
    }
    for target in TARGETS:
        questions = scoped(all_questions, target, unjudged)
        c = fitted["choices"][target]
        knn = NearestNeighbours(dev_q, dev_m, MODELS, target, k=c["knn"]["k"], tau=c["knn"]["tau"])
        by_type = fit_by_type(dev_q, dev_m, MODELS, target)
        assert dict(by_type.choice) == c["by_type"], "by-type refit differs from the frozen fit"
        routers = [
            *(SingleModel(m) for m in MODELS),
            by_type,
            Cascade("haiku", "sonnet"),
            knn,
        ]
        results = {r.name: evaluate(r, questions, matrix, target) for r in routers}
        results["oracle"] = oracle(questions, matrix, MODELS, target)
        chosen = results[c["chosen"]]
        best = results[SingleModel(c["always_best"]).name]
        haiku = results[SingleModel("haiku").name]
        vs_best = paired_bootstrap_diff(best.values, chosen.values)
        vs_haiku = paired_bootstrap_diff(haiku.values, chosen.values)
        b = sum(h and not x for h, x in zip(haiku.values, chosen.values, strict=True))
        cc = sum(x and not h for h, x in zip(haiku.values, chosen.values, strict=True))
        same = chosen.name == best.name
        out[target] = {
            "routers": {name: summary(r) for name, r in results.items()},
            "chosen": c["chosen"],
            "always_best": c["always_best"],
            "chosen_minus_best": {
                "diff": round(vs_best.mean, 4),
                "low": round(vs_best.low, 4),
                "high": round(vs_best.high, 4),
            },
            "cost_share_of_best": round(chosen.cost / best.cost, 4),
            "as_good_for_less": (not same) and vs_best.low > MARGIN and chosen.cost < best.cost,
            "chosen_minus_haiku": {
                "diff": round(vs_haiku.mean, 4),
                "low": round(vs_haiku.low, 4),
                "high": round(vs_haiku.high, 4),
                "mcnemar_p": round(mcnemar_exact(b, cc), 4),
            },
            "better_than_haiku": vs_haiku.low > 0,
        }
    out["matrix_by_type"] = by_type_table(all_questions, matrix, unjudged)
    out["models"] = extra
    TEST_RESULT.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {t: {k: v for k, v in out[t].items() if k != "routers"} for t in TARGETS}, indent=1
        )
    )
    print(f"wrote {TEST_RESULT}")


def spot_sample(store: ResultsStore, model: str) -> list[str]:
    """The pre-registered 30: answered attempt-1 dev answers, in sha256 order of salt, model and
    case id."""
    pool = sorted(
        (a for a in store.answered_attempts(RUNS["dev"][model]) if a["attempt"] == 1),
        key=lambda a: hashlib.sha256((SPOT_SALT + model + ":" + a["case_id"]).encode()).hexdigest(),
    )
    return [a["case_id"] for a in pool[:30]]


def spot() -> None:
    store = ResultsStore(Path("results/evals.duckdb"))
    try:
        for model in ("sonnet", "opus"):
            ids = spot_sample(store, model)
            print(f"{model} spot-check sample ({len(ids)}): {ids}")
            print(
                "  uv run evals label "
                + RUNS["dev"][model]
                + f" --labeler {LABELER} "
                + " ".join(f"--case {c}" for c in ids)
            )
    finally:
        store.close()


def spot_score() -> None:
    """The plan's rule: a model's judge numbers hold if the judge passes at most 2 of its 30
    answers that the labeler fails on fully supported."""
    out: dict[str, Any] = {}
    store = ResultsStore(Path("results/evals.duckdb"))
    try:
        for model in ("sonnet", "opus"):
            run_id = RUNS["dev"][model]
            ids = spot_sample(store, model)
            labels = {
                x["case_id"]: x["supported"]
                for x in store.labels(run_id, LABELER)
                if x["attempt"] == 1 and x["supported"] is not None
            }
            missing = [c for c in ids if c not in labels]
            if missing:
                raise SystemExit(
                    f"{model}: {len(missing)} of 30 not labeled yet ({missing[0]}, ...)"
                )
            judged = store.grades_for(run_id, JUDGE)
            pairs = [(judged[(c, 1)]["judge_supported"] == 1.0, bool(labels[c])) for c in ids]
            false_passes = sum(j and not h for j, h in pairs)
            out[model] = {
                "run": run_id,
                "false_passes": false_passes,
                "false_fails": sum(h and not j for j, h in pairs),
                "agreement": sum(j == h for j, h in pairs),
                "n": len(pairs),
                "trusted": false_passes <= 2,
            }
    finally:
        store.close()
    (OUT / "spot-check.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    {"fit": fit, "evaluate": evaluate_test, "spot": spot, "spot-score": spot_score}[sys.argv[1]]()
