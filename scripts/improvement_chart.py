"""Build the README charts from the results store: docs/charts/improvement.svg (the gated loop),
docs/charts/causes.svg (why answers are not fully supported) and docs/charts/improvement.json (every
plotted number, so the charts can be checked without the store).

Usage: uv run python -I -X utf8 scripts/improvement_chart.py
Reads results/evals.duckdb (no label session may be open) and the error-analysis files.
"""

import json
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.chart import Interval, VersionRow, render_causes, render_improvement
from agent_evals.report import compare, summarize
from agent_evals.stats.core import wilson_ci
from agent_evals.store.db import ResultsStore

JUDGE = "judge"  # the validated judge, v2 (2cf75fa03e09)
# (label, status, reason, run, is the judge trusted on this version's answers)
HOLDOUT = [
    ("v2 baseline", "baseline", "", "docs-20261011-225715-b67388", True),
    ("v3: cite every sentence (iteration 1)", "shipped", "", "docs-20261016-222256-04f0f4", True),
]
DEV = [
    ("v2 baseline", "baseline", "", "docs-20261011-223111-702e87", True),
    (
        "v3: cite every sentence (iteration 1)",
        "shipped",
        "passed the gate and the test split",
        "docs-20261016-214710-02abb7",
        True,
    ),
    (
        "number check at submit (iteration 2)",
        "rejected",
        "gain not detectable; more rejected submissions",
        "docs-20261016-225705-5447b9",
        True,
    ),
    (
        "v4: check each sentence (iteration 3)",
        "rejected",
        "more wrong abstentions; lookup slipped",
        "docs-20261017-163240-f5782a",
        False,
    ),
    (
        "v5: v4 + abstention guard (iteration 4)",
        "not shipped",
        "passed the gate; judge not trusted on it",
        "docs-20261017-174739-1c0e8e",
        False,
    ),
]
CAUSES = {
    "unquoted_on_page": "on a page it read, not quoted",
    "beyond_quote": "goes beyond its quote",
    "about_the_docs": "about the docs themselves",
    "headerless_row": "table value without its header",
    "not_in_read_pages": "on no page it read",
}
TAXONOMY = [
    ("v3", "results/iteration-3/error-analysis.json"),
    ("v4", "results/iteration-3/v4-error-analysis.json"),
    ("v5", "results/iteration-4/v5-error-analysis.json"),
]
OUT = Path("docs/charts")


def iv(est: Any) -> Interval:
    return Interval(est.mean, est.low, est.high, est.n)


def human(store: ResultsStore, run_id: str) -> Interval | None:
    labels = [x for x in store.labels(run_id, "aleena") if x["supported"] is not None]
    if not labels:
        return None
    ci = wilson_ci(sum(1 for x in labels if x["supported"]), len(labels))
    return Interval(ci.mean, ci.low, ci.high, len(labels))


store = ResultsStore(Path("results/evals.duckdb"))
try:
    data: dict[str, Any] = {"judge": "v2 (2cf75fa03e09)", "holdout": [], "dev": []}
    for key, spec in (("holdout", HOLDOUT), ("dev", DEV)):
        for label, status, reason, rid, trusted in spec:
            s = summarize(store, rid, JUDGE)
            h = None if key == "holdout" else human(store, rid)
            entry = {
                "label": label,
                "status": status,
                "reason": reason,
                "run": rid,
                "judge_trusted": trusted,
                "fully": asdict(iv(s.metrics["judge_supported"])),
                "correct": asdict(iv(s.metrics["correct"])),
                "human_fully": asdict(h) if h else None,
            }
            if key == "holdout":  # dev costs moved with the keyword regrade; only holdout plotted
                entry["cost_per_success"] = s.cost_per_success
            data[key].append(entry)
    pair = {
        m: compare(store, HOLDOUT[0][3], HOLDOUT[1][3], m, JUDGE)
        for m in ("judge_supported", "correct")
    }
    data["holdout_paired"] = {
        m: {
            "diff": [c.diff.mean, c.diff.low, c.diff.high],
            "p": c.p_value,
            "n": c.n_cases,
            "verdict": c.verdict,
        }
        for m, c in pair.items()
    }
    v5_labels = [x for x in store.labels(DEV[-1][3], "aleena") if x["supported"] is not None]
    data["v5_human_failing"] = [sum(1 for x in v5_labels if not x["supported"]), len(v5_labels)]
finally:
    store.close()

data["causes"] = []
for label, path in TAXONOMY:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    by_kind = doc["counts"]["by_kind"] if "counts" in doc else doc["aggregate"]["by_kind"]
    data["causes"].append(
        {
            "label": label,
            "answered": doc["answers"]["attempt1_answered"],
            "failing": doc["answers"]["failing_fully_supported"],
            "counts": {CAUSES[k]: by_kind[k]["answers_with"] for k in CAUSES},
        }
    )
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "improvement.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def row(d: dict[str, Any]) -> VersionRow:
    def get(k: str) -> Interval | None:
        return Interval(**d[k]) if d.get(k) else None

    return VersionRow(
        d["label"],
        d["status"],
        get("fully"),
        get("correct"),
        get("human_fully"),
        d.get("reason", ""),
        d.get("judge_trusted", True),
    )


fp = data["holdout_paired"]["judge_supported"]
cps = [d["cost_per_success"] for d in data["holdout"]]
d0, lo, hi = (100 * v for v in fp["diff"])
nh = [d["fully"]["n"] for d in data["holdout"]]
nd = [d["fully"]["n"] for d in data["dev"]]
fail5, n5 = data["v5_human_failing"]
notes = [
    f"v2 to v3, paired on the {fp['n']} cases both runs answered with citations: fully supported "
    f"{d0:+.0f} points (95% CI {lo:+.0f} to {hi:+.0f}, p = {fp['p']:.0e}).",
    f"Correct: no detectable change. Cost per successful answer: about ${cps[0]:.4f} for both "
    f"(${cps[0]:.6f} v2, ${cps[1]:.6f} v3). v4 and v5 never reached the holdout.",
]
foot_paragraphs = [
    "Correct: the answer states the required facts (keyword check), or abstains when it should, "
    "over all cases.",
    "Fully supported: every claim in the answer is backed by a quote it cites, judged by the "
    "project's LLM judge (validated against a human, kappa 0.80 to 1.00) over answered cases with "
    f"citations: holdout {nh[0]} and {nh[1]} of 150, dev {min(nd)} to {max(nd)} of 130. Dev dots "
    "average 3 attempts per case; the CHANGELOG pairs the cases two runs share, so its figures "
    "differ slightly (v5: 0.612 paired, 0.606 here). Dev correct is after the 2026-10-17 keyword "
    "regrade; every gate decision was re-read and stood.",
    "Gate: fully supported must improve beyond its interval, and correct, abstentions, fabricated "
    "citations, rejected submissions and every case type and area must not regress.",
    "v4 and v5 write 27-28% shorter answers, past the 25% line fixed before the runs, so the judge "
    "needed a human spot check. For v5 it failed (the judge passed 3 of 30 answers the human "
    "failed; the bar was 2), and two repairs of the judge also missed their rules, so v5 was not "
    "shipped. v4 was never spot-checked.",
    "Human labels (diamonds), attempt 1: v2 114 and v3 80 answers, the first in case-id order; v5 "
    "90 answers, a random sample. The samples differ, so the diamonds are not a paired comparison.",
    "Data: docs/charts/improvement.json, rebuilt by scripts/improvement_chart.py.",
]
causes_paragraphs = [
    "Dev split, attempt 1, answered attempts. Failing answers are those the LLM judge fails on "
    "fully supported. The judge is not trusted on v4 and v5's shorter answers and passes some "
    f"failures there, so their bars undercount: by human labels, v5 fails {fail5} of {n5} "
    f"({100 * fail5 / n5:.0f}%).",
    "Causes were classified by reader agents that checked the pages each answer's agent had read. "
    "A second reading of 15 v3 answers gave identical causes for 11. 'Other' (one v3 answer) is "
    "omitted, and v2 was not classified this way. v4 and v5 were not shipped; they show what the "
    "stricter citation rules changed.",
]


def wrap(paragraphs: list[str], width: int = 108) -> list[str]:
    return [line for para in paragraphs for line in textwrap.wrap(para, width)]


(OUT / "improvement.svg").write_text(
    render_improvement(
        [row(d) for d in data["holdout"]],
        [row(d) for d in data["dev"]],
        notes,
        wrap(foot_paragraphs),
    ),
    encoding="utf-8",
)
(OUT / "causes.svg").write_text(
    render_causes(
        list(CAUSES.values()),
        [(c["label"], c["answered"], c["failing"], c["counts"]) for c in data["causes"]],
        wrap(causes_paragraphs),
    ),
    encoding="utf-8",
)
print(json.dumps(data["holdout_paired"], indent=1))
for d in data["dev"]:
    h = d["human_fully"]
    print(
        f"{d['label']:<42} fully {d['fully']['mean']:.3f} (n={d['fully']['n']})  "
        f"correct {d['correct']['mean']:.3f}"
        + (f"  human {h['mean']:.3f} (n={h['n']})" if h else "")
    )
print("v5 human failing:", data["v5_human_failing"])
print("written:", sorted(p.name for p in OUT.iterdir()))
