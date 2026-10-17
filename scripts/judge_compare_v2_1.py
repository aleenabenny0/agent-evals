"""Score judge v2.1 against v2 under the rule in docs/judge-validation.md section 9, once.

Criterion 1: the 30 fresh labels (positions 31-60 of the spot-check hash order on the v5 dev run).
Criterion 2: the earlier label sets (114 on the first dev run, 80 on the v3 champion's run, the 30
spot-check labels), against the stored v2 verdicts. Criterion 3: read from the paired probe report.
Agreement is per verdict over labeled attempts with citations; ties count as "at least as many";
a false pass is a judge pass on an answer the labeler failed.

Usage: uv run python -I -X utf8 scripts/judge_compare_v2_1.py PROBES.json OUT.json
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.stats.core import cohens_kappa
from agent_evals.store.db import ResultsStore

V2, V21 = "judge", "judge-v2.1"
V5 = "docs-20261017-174739-1c0e8e"
FRESH = {
    "docs-ai-005-p1",
    "docs-ai-002",
    "docs-int-020",
    "docs-mix-055",
    "docs-chg-023",
    "docs-ai-005",
    "docs-ai-005-p2",
    "docs-int-041",
    "docs-ai-033",
    "docs-mix-056",
    "docs-core-011",
    "docs-ai-029",
    "docs-chg-013",
    "docs-int-029",
    "docs-ai-002-p2",
    "docs-int-007",
    "docs-core-034",
    "docs-mix-050",
    "docs-core-018",
    "docs-core-025",
    "docs-ai-038",
    "docs-core-039",
    "docs-mix-060",
    "docs-mix-085",
    "docs-chg-010",
    "docs-ai-001",
    "docs-chg-019",
    "docs-ai-007",
    "docs-core-016",
    "docs-mix-077",
}
SETS = {
    "first dev run (114)": ("docs-20261011-223111-702e87", None),
    "v3 champion run (80)": ("docs-20261016-214710-02abb7", None),
    "spot check (30)": (V5, "spot"),
    "fresh (30)": (V5, "fresh"),
}
VERDICTS = (
    ("fully", "supported", "judge_supported"),
    ("core", "core_supported", "judge_core_supported"),
)
probes_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])

store = ResultsStore(Path("results/evals.duckdb"))
try:
    data = {}
    for name, (rid, part) in SETS.items():
        labels = store.labels(rid, "aleena")
        if part == "fresh":
            labels = [x for x in labels if x["case_id"] in FRESH]
        elif part == "spot":
            labels = [x for x in labels if x["case_id"] not in FRESH]
        data[name] = (labels, store.grades_for(rid, V2), store.grades_for(rid, V21))
finally:
    store.close()


def score(labels: list[dict[str, Any]], g2: dict, g21: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for short, field, metric in VERDICTS:
        rows = [
            (bool(x[field]), g2[k][metric] >= 1.0, g21[k][metric] >= 1.0, x["case_id"])
            for x in labels
            if x.get(field) is not None
            and metric in g2.get(k := (x["case_id"], x["attempt"]), {})
            and metric in g21.get(k, {})
        ]
        h = [r[0] for r in rows]
        out[short] = {
            "n": len(rows),
            "agree_v2": sum(r[0] == r[1] for r in rows),
            "agree_v21": sum(r[0] == r[2] for r in rows),
            "false_pass_v2": sum((not r[0]) and r[1] for r in rows),
            "false_pass_v21": sum((not r[0]) and r[2] for r in rows),
            "false_fail_v2": sum(r[0] and not r[1] for r in rows),
            "false_fail_v21": sum(r[0] and not r[2] for r in rows),
            "kappa_v2": cohens_kappa(h, [r[1] for r in rows]) if rows else None,
            "kappa_v21": cohens_kappa(h, [r[2] for r in rows]) if rows else None,
            "judges_differ": [r[3] for r in rows if r[1] != r[2]],
        }
    return out


report: dict[str, Any] = {name: score(*vals) for name, vals in data.items()}
fresh = report["fresh (30)"]
identical = not fresh["fully"]["judges_differ"]
c1 = (
    fresh["fully"]["agree_v21"] >= fresh["fully"]["agree_v2"]
    and fresh["core"]["agree_v21"] >= fresh["core"]["agree_v2"]
    and fresh["fully"]["false_pass_v21"] <= fresh["fully"]["false_pass_v2"]
    and fresh["fully"]["false_pass_v21"] <= 2
)
earlier = ["first dev run (114)", "v3 champion run (80)", "spot check (30)"]
c2_per_set = all(
    report[s][v]["agree_v21"] >= report[s][v]["agree_v2"] - 1
    for s in earlier
    for v in ("fully", "core")
)
c2_sum = all(
    sum(report[s][v]["agree_v21"] for s in earlier)
    >= sum(report[s][v]["agree_v2"] for s in earlier)
    for v in ("fully", "core")
)
probes = json.loads(probes_path.read_text(encoding="utf-8"))
c3 = bool(probes["criterion_3"]["holds"])
report["decision"] = {
    "criterion_1_fresh": c1,
    "fresh_verdicts_identical_label_61_to_90": identical,
    "criterion_2_per_set": c2_per_set,
    "criterion_2_summed": c2_sum,
    "criterion_3_probes": c3,
    "adopt_v2_1": c1 and not identical and c2_per_set and c2_sum and c3,
}
out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
for name in SETS:
    for v in ("fully", "core"):
        r = report[name][v]
        print(
            f"{name:<22} {v:<5} n={r['n']:<3} agree v2 {r['agree_v2']:<3} v2.1 {r['agree_v21']:<3} "
            f"false pass {r['false_pass_v2']}->{r['false_pass_v21']}  "
            f"false fail {r['false_fail_v2']}->{r['false_fail_v21']}  differ {r['judges_differ']}"
        )
print(
    "\nprobes:",
    {p: {n: f"{s['ok']}/{s['n']}" for n, s in by.items()} for p, by in probes["probes"].items()},
)
print("\nDECISION (rule committed in d645d5d):", report["decision"])
