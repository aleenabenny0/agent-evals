"""A model of selection on the exploration passes, fitted to revision 1's saved passes: how often a
version like revision 1 looks eligible on one pass, on three, and on fresh passes after a choice
among several such versions. An illustration of the process, not a measurement of any judge; the
proposal (docs/plans/judge-v6.md) cites it for why the choice is made on fresh salts.

    uv run python scripts/judge_v5_selection_model.py

The model, in full. The judge under test is revision 1 at t = 70 with s off. Each answer outside
v5's named cases errs, independently on every pass, with the share of revision 1's three saved
passes (results/judge-v5/stage-a-rev1*.json) in which it erred there: four answers have such a
rate (1.0 as a false pass, 1.0 as a false fail, 0.67 as a false fail and 0.33 as a false pass,
read from the files), and every other answer never errs, so the model understates the noise. v2 is
fixed at its observed three-pass means, 0.67 errors and 0.33 false fails per pass, and a version
is eligible when its mean errors are at most v2's plus 2 and its mean false fails at most v2's
plus 1, as stage A reads the caps. K versions are independent copies with the same rates (equally
good, so choosing among them is pure noise); each is explored on n passes, any version that looks
eligible is chosen, and the chosen one is then judged again on three fresh passes. The random
numbers come from random.Random(20261029); 200,000 rounds for each pass count on its own, and
50,000 rounds for each pair of n and K. Nothing is judged: the rates are read from the saved
passes with the claim judge's own code, and no call is made.

Output, printed and written to results/judge-v5/analysis/selection-model.json: the rates and the
true per-pass means they imply; P(one version looks eligible) on 1, 2, 3 and 6 passes; and for
n in {1, 3} and K in {1, 2, 4}, P(any of the K looks eligible on its exploration passes), P(a
version chosen that way is still eligible on three fresh passes), and the mean of the
best-looking version's errors as read on its exploration passes less the true mean (negative: it
reads low). Run from the repository root.
"""

from __future__ import annotations

import json
import random
from fractions import Fraction
from pathlib import Path
from typing import Any

from agent_evals.graders.claim_judge import Assessment

RESULTS = Path("results/judge-v5")
OUT = RESULTS / "analysis" / "selection-model.json"
PASS_FILES = ("stage-a-rev1.json", "stage-a-rev1.judge-s2.json", "stage-a-rev1.judge-s3.json")
T, S = 70, None  # revision 1's candidate cell
V2_ERRORS, V2_FALSE_FAILS = Fraction(2, 3), Fraction(1, 3)  # v2's three-pass means per pass
CAP_ERRORS, CAP_FALSE_FAILS = V2_ERRORS + 2, V2_FALSE_FAILS + 1
SEED = 20261029
ROUNDS = 200_000
PASS_COUNTS = (1, 2, 3, 6)
EXPLORATION_PASSES = (1, 3)
VERSIONS = (1, 2, 4)
FRESH_PASSES = 3

Rate = tuple[float, bool]  # the probability of an error on a pass, and whether it is a false fail
Means = tuple[Fraction, Fraction]  # mean errors and mean false fails over some passes


def error_rates() -> tuple[list[dict[str, Any]], list[Rate]]:
    """The answers outside v5's named cases that revision 1 got wrong at t = 70 with s off on at
    least one of its three saved passes, each with the share of passes it erred on. Read in the
    pass files' order of answers, which fixes the order the random numbers are drawn in."""
    held = [json.loads((RESULTS / name).read_text(encoding="utf-8")) for name in PASS_FILES]
    notes = {str(a["key"]): a for a in held[0]["answers"]}
    rows: list[dict[str, Any]] = []
    rates: list[Rate] = []
    for key, note in notes.items():
        if note["named"]:
            continue
        supported = bool(note["supported"])
        wrong = 0
        for raw in held:
            verdict = Assessment.model_validate(raw["assessments"][key]).verdict(T, S)
            wrong += verdict.fully_supported != supported
        if wrong:
            rows.append(
                {
                    "key": key,
                    "label_supported": supported,
                    "errors_over_three_passes": wrong,
                    "rate": round(wrong / len(held), 4),
                    "kind": "false fail" if supported else "false pass",
                }
            )
            rates.append((wrong / len(held), supported))
    return rows, rates


def one_pass(rng: random.Random, rates: list[Rate]) -> tuple[int, int]:
    """One pass of a version: its errors and, of them, its false fails."""
    errors = false_fails = 0
    for p, false_fail in rates:
        if rng.random() < p:
            errors += 1
            false_fails += false_fail
    return errors, false_fails


def judged(rng: random.Random, rates: list[Rate], passes: int) -> Means:
    """A version's means over this many passes, as exact fractions."""
    errors = false_fails = 0
    for _ in range(passes):
        e, f = one_pass(rng, rates)
        errors += e
        false_fails += f
    return Fraction(errors, passes), Fraction(false_fails, passes)


def eligible(means: Means) -> bool:
    return means[0] <= CAP_ERRORS and means[1] <= CAP_FALSE_FAILS


def simulate(rates: list[Rate]) -> dict[str, Any]:
    true_errors = sum(p for p, _ in rates)
    true_false_fails = sum(p for p, false_fail in rates if false_fail)
    rng = random.Random(SEED)
    one_version = {}
    for passes in PASS_COUNTS:
        hits = sum(eligible(judged(rng, rates, passes)) for _ in range(ROUNDS))
        one_version[str(passes)] = round(hits / ROUNDS, 4)
    rounds = ROUNDS // 4
    chosen = []
    for passes in EXPLORATION_PASSES:
        for k in VERSIONS:
            looks_eligible = still_eligible = 0
            bias = 0.0
            for _ in range(rounds):
                looks = [judged(rng, rates, passes) for _ in range(k)]
                if any(eligible(m) for m in looks):
                    looks_eligible += 1
                    if eligible(judged(rng, rates, FRESH_PASSES)):  # the chosen one, re-judged
                        still_eligible += 1
                best = min(looks)  # the fewest errors among the K, as read
                bias += float(best[0]) - true_errors
            chosen.append(
                {
                    "exploration_passes": passes,
                    "versions": k,
                    "rounds": rounds,
                    "p_any_looks_eligible": round(looks_eligible / rounds, 4),
                    "p_chosen_still_eligible_on_3_fresh_passes": round(still_eligible / rounds, 4),
                    "best_looking_errors_minus_true_mean": round(bias / rounds, 4),
                }
            )
    return {
        "true_means_per_pass": {
            "errors": round(true_errors, 4),
            "false_fails": round(true_false_fails, 4),
        },
        "caps_per_pass": {
            "errors": float(CAP_ERRORS),
            "false_fails": float(CAP_FALSE_FAILS),
            "v2_means": {"errors": float(V2_ERRORS), "false_fails": float(V2_FALSE_FAILS)},
        },
        "p_one_version_looks_eligible_by_passes": one_version,
        "choice_among_k_versions": chosen,
    }


def main() -> int:
    rows, rates = error_rates()
    result = {
        "about": (
            "A model of selection on the exploration passes, fitted to revision 1's three saved "
            "passes at t = 70 with s off, by scripts/judge_v5_selection_model.py (its docstring "
            "states the model in full). An illustration, not a measurement: nothing was judged, "
            "and no call was made."
        ),
        "model": {
            "judge": f"revision 1 at t = {T}, s off",
            "answers_with_an_error_rate": rows,
            "every_other_answer": "never errs",
            "versions": "K independent copies with the same rates",
            "seed": SEED,
            "rounds": {"per_pass_count": ROUNDS, "per_n_and_k": ROUNDS // 4},
        },
        **simulate(rates),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print("answers with an error rate above 0:")
    for row in rows:
        print(f"  {row['key']}: {row['kind']} at {row['rate']}")
    means = result["true_means_per_pass"]
    print(
        f"true per-pass means in the model: errors {means['errors']}, false fails "
        f"{means['false_fails']}"
    )
    caps = result["caps_per_pass"]
    print(f"caps: errors <= {caps['errors']:.2f}, false fails <= {caps['false_fails']:.2f}")
    for passes, p in result["p_one_version_looks_eligible_by_passes"].items():
        print(f"  P(one such version looks eligible on {passes} pass(es)) = {p:.3f}")
    print("K equally good versions, each explored on n passes; any that looks eligible is chosen:")
    for row in result["choice_among_k_versions"]:
        print(
            f"  n={row['exploration_passes']} K={row['versions']}: looks eligible "
            f"{row['p_any_looks_eligible']:.3f}; still eligible on 3 fresh passes "
            f"{row['p_chosen_still_eligible_on_3_fresh_passes']:.3f}; best-looking errors read "
            f"{row['best_looking_errors_minus_true_mean']:+.2f} against the true mean"
        )
    print(f"written: {OUT.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
