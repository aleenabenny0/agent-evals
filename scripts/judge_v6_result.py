"""The figures of judge v6's result (docs/plans/judge-v6.md, "Result"), recomputed from the round's
records in results/judge-v6/ and written to results/judge-v6/analysis.json.

    uv run python scripts/judge_v6_result.py

Nothing is judged and no call is made. Every figure re-reads the saved passes: the verdicts each
pass's code saved at every cell, and the assessments, parsed by the claim judge's own code, for
what the gap rule moved. v2's three passes come from the pass files' stored verdicts and the record
of its two replayed passes. Most of it is post hoc: a cell that was no candidate decides nothing,
and is read here only to say what the stop did and did not depend on. Run from the repository root.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_evals.graders.claim_judge import T_GRID, Assessment

OUT = Path("results/judge-v6")
V2_EXTRA = Path("results/judge-v5/analysis/v2-extra-passes.json")
ADDED = {"docs-ai-003-p1", "docs-ai-003-p3", "docs-chg-010", "docs-mix-042", "docs-mix-058"}
KNOWN_MISS = "docs-mix-058"
BORDERLINE = "docs-20261017-174739-1c0e8e/docs-chg-010"  # the kept answer behind the stop
CANDIDATES = {"draft": "t=64 s=49", "rev1": "t=69 s=off"}
CELLS = [f"t={t} s={s}" for t in T_GRID for s in ("off", 49, 59)]
PASSES = (1, 2, 3)
Passes = list[dict[str, Any]]


def load(path: Path) -> dict[str, Any]:
    held: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return held


def passes(label: str, kind: str) -> Passes:
    return [load(OUT / f"stage-a-{label}.{kind}{n}.json") for n in PASSES]


def verdicts(held: Mapping[str, Any], cell: str) -> dict[str, bool | None]:
    return {
        key: None if found["judge_error"] else found["cells"][cell]["fully_supported"]
        for key, found in held["verdicts"].items()
    }


def errors(
    per_pass: Sequence[Mapping[str, bool | None]], notes: Mapping[str, Any], keys: Iterable[str]
) -> dict[str, Any]:
    wanted = list(keys)
    rows = []
    for held in per_pass:
        fp = sum(held[k] is True and not notes[k]["supported"] for k in wanted)
        ff = sum(held[k] is False and notes[k]["supported"] for k in wanted)
        je = sum(held[k] is None for k in wanted)
        rows.append({"false_passes": fp, "false_fails": ff, "judge_errors": je})
    fp = sum(r["false_passes"] for r in rows)
    ff = sum(r["false_fails"] for r in rows)
    je = sum(r["judge_errors"] for r in rows)
    return {
        "per_pass": [r["false_passes"] + r["false_fails"] + r["judge_errors"] for r in rows],
        "false_passes": fp,
        "false_fails": ff,
        "judge_errors": je,
        "errors": fp + ff + je,
        "per_pass_mean": round((fp + ff + je) / len(rows), 4),
    }


def sides(notes: Mapping[str, Any]) -> dict[str, list[str]]:
    named = [k for k in notes if notes[k]["named"]]
    return {
        "all_125": list(notes),
        "capped_93": [k for k in notes if k not in named],
        "named_32": named,
    }


def kept(notes: Mapping[str, Any]) -> list[str]:
    return [
        k
        for k, n in notes.items()
        if n["named"] and n["supported"] and n["v2_passed"] and not k.endswith(KNOWN_MISS)
    ]


def read_cell(
    per_pass: Sequence[Mapping[str, bool | None]], notes: Mapping[str, Any]
) -> dict[str, Any]:
    found = {name: errors(per_pass, notes, keys) for name, keys in sides(notes).items()}
    held = {k: sum(p[k] is True for p in per_pass) for k in kept(notes)}
    found["kept_short"] = sorted(k for k, n in held.items() if n < 2)
    found["passes"] = [sum(v is True for v in p.values()) for p in per_pass]
    found["fail_rates"] = [round(1 - n / len(notes), 3) for n in found["passes"]]
    found["false_pass_share_of_passes"] = round(
        found["all_125"]["false_passes"] / sum(found["passes"]), 4
    )
    return found


def v2_reading(notes: Mapping[str, Any]) -> dict[str, Any]:
    stored: dict[str, bool | None] = {k: bool(n["v2_passed"]) for k, n in notes.items()}
    extra: dict[str, dict[str, bool | None]] = {}
    for row in load(V2_EXTRA)["verdicts"]:
        extra.setdefault(row["salt"], {})[row["key"]] = row["judge_supported"]
    return read_cell([stored, *extra.values()], notes)


def errs_by_answer(
    per_pass: Sequence[Mapping[str, bool | None]], notes: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    for key, note in notes.items():
        wrong = sum(p[key] is None or p[key] is not bool(note["supported"]) for p in per_pass)
        if wrong:
            rows.append(
                {
                    "key": key,
                    "kind": "false fail" if note["supported"] else "false pass",
                    "named": note["named"],
                    "of_three": wrong,
                }
            )
    return rows


def borderline(label: str, kind: str, cell: str) -> list[dict[str, Any]]:
    rows = []
    for held in passes(label, kind):
        found = Assessment.model_validate(held["assessments"][BORDERLINE])
        weakest = min(found.claims, key=lambda c: c.score)
        search = found.skeptic.search if found.skeptic else None
        rows.append(
            {
                "salt": held["salt"],
                "weakest_claim_score": weakest.score,
                "weakest_claim_gap": weakest.gap,
                "search_confidence": None
                if search is None or not search.found
                else search.confidence,
                "passes_at_the_cell": held["verdicts"][BORDERLINE]["cells"][cell][
                    "fully_supported"
                ],
            }
        )
    return rows


def without_the_rule(found: Assessment) -> Assessment:
    """The same judging with the gap types ignored: every claim scored, as v5 would read it."""
    plain = [c.model_copy(update={"gap": None, "term": ""}) for c in found.claims]
    return found.model_copy(update={"claims": plain})


def gap_rule(notes: Mapping[str, Any]) -> dict[str, Any]:
    """What the gap types did on revision 1's six passes at t = 69 with s off: the verdicts the
    rule moved from pass to fail against reading the same replies with the scores alone, right
    (labeled unsupported) and wrong (labeled supported), and the claims by gap type and label."""
    by_gap: Counter[str] = Counter()
    moved: Counter[str] = Counter()
    answers: Counter[str] = Counter()
    wrong_answers: Counter[str] = Counter()
    for held in [*passes("rev1", "pass"), *passes("rev1", "choice")]:
        for key, raw in held["assessments"].items():
            found = Assessment.model_validate(raw)
            if found.error is not None:
                continue
            side = "supported" if notes[key]["supported"] else "unsupported"
            for claim in found.claims:
                by_gap[f"{claim.gap} {side}"] += 1
            if found.verdict(69, None).fully_supported:
                continue
            if without_the_rule(found).verdict(69, None).fully_supported:
                moved[side] += 1
                (answers if side == "unsupported" else wrong_answers)[key] += 1
    return {
        "judgings": 750,
        "moved_pass_to_fail": dict(moved),
        "answers_rightly_failed": len(answers),
        "answers_wrongly_failed": sorted(wrong_answers),
        "claims_by_gap_and_label": dict(sorted(by_gap.items())),
    }


def clearing(notes: Mapping[str, Any]) -> dict[str, Any]:
    """What code cleared over all twelve passes, and the verdicts that moved at each candidate's
    own cell against reading the same judging with no names to clear."""
    flags: Counter[str] = Counter()
    moved: list[dict[str, Any]] = []
    for label, cell in (("draft", (64, 49)), ("rev1", (69, None))):
        for held in [*passes(label, "pass"), *passes(label, "choice")]:
            for key, raw in held["assessments"].items():
                found = Assessment.model_validate(raw)
                if found.error is not None or not found.cleared_names():
                    continue
                for item in found.cleared_names():
                    flags[f"{key} {item.token}: {item.context}"] += 1
                now = found.verdict(*cell).fully_supported
                bare = found.model_copy(update={"cited_names": []})
                if now != bare.verdict(*cell).fully_supported:
                    moved.append(
                        {
                            "version": label,
                            "salt": held["salt"],
                            "key": key,
                            "to_pass": now,
                            "right": now == notes[key]["supported"],
                        }
                    )
    return {"flags_cleared": dict(flags), "verdicts_moved": moved}


def v2_on_the_remaining_errors(
    notes: Mapping[str, Any], remaining: Iterable[str]
) -> dict[str, int]:
    """How many of v2's three passes err on each answer revision 1 got wrong in exploration."""
    extra: dict[str, list[bool | None]] = {}
    for row in load(V2_EXTRA)["verdicts"]:
        extra.setdefault(row["key"], []).append(row["judge_supported"])
    return {
        key: sum(
            v is not bool(notes[key]["supported"]) for v in [notes[key]["v2_passed"], *extra[key]]
        )
        for key in remaining
    }


def main() -> None:
    first = passes("rev1", "pass")[0]
    notes = {a["key"]: a for a in first["answers"]}
    ledger = load(OUT / "spend.json")
    choice = load(OUT / "stage-a-choice.json")
    v2 = v2_reading(notes)
    versions: dict[str, Any] = {}
    for label, cell in CANDIDATES.items():
        explored = [verdicts(p, cell) for p in passes(label, "pass")]
        fresh = passes(label, "choice")
        at_cell = [verdicts(p, cell) for p in fresh]
        versions[label] = {
            "candidate_cell": cell,
            "exploration": read_cell(explored, notes),
            "fresh": read_cell(at_cell, notes),
            "fresh_errors_by_answer": errs_by_answer(at_cell, notes),
            "fresh_every_cell_post_hoc": {
                name: read_cell([verdicts(p, name) for p in fresh], notes) for name in CELLS
            },
            "borderline_answer": {
                "exploration": borderline(label, "pass", cell),
                "fresh": borderline(label, "choice", cell),
            },
        }
    record = {
        "about": (
            "Judge v6's result, recomputed from its records by scripts/judge_v6_result.py. "
            "Nothing was judged and no call was made. The stop is stage-a-choice.json's. Every "
            "other cell here is post hoc: it decides nothing."
        ),
        "stop": choice["stop"],
        "frozen": choice["frozen"],
        "spend": {"total_usd": ledger["total_usd"], "cap_usd": ledger["cap_usd"]},
        "v2": v2,
        "versions": versions,
        "gap_rule": gap_rule(notes),
        "clearing": clearing(notes),
        "v2_on_rev1s_exploration_errors": v2_on_the_remaining_errors(
            notes,
            [
                e["key"]
                for e in errs_by_answer(
                    [verdicts(p, CANDIDATES["rev1"]) for p in passes("rev1", "pass")], notes
                )
            ],
        ),
    }
    path = OUT / "analysis.json"
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"written: {path}")


if __name__ == "__main__":
    main()
