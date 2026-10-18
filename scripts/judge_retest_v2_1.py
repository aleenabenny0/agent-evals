"""Score the judge v2.1 retest once, under docs/judge-validation.md section 10.

Criterion 1 and the core guard on the new 30 labels (hash positions 61-90 on the v5 dev run);
criterion 3 on the four earlier label sets (fully supported only); criterion 4 carried over from
section 9's probe report.

Usage: uv run python -I -X utf8 scripts/judge_retest_v2_1.py SECTION9_PROBES.json OUT.json
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.store.db import ResultsStore

V2, V21 = "judge", "judge-v2.1"
V5 = "docs-20261017-174739-1c0e8e"
NEW = {
    "docs-ai-023",
    "docs-int-034",
    "docs-chg-001",
    "docs-ai-004-p3",
    "docs-mix-067",
    "docs-chg-027",
    "docs-int-037",
    "docs-mix-090",
    "docs-int-045",
    "docs-mix-022",
    "docs-ai-003-p3",
    "docs-ai-005-p3",
    "docs-ai-036",
    "docs-ai-010",
    "docs-chg-003",
    "docs-mix-042",
    "docs-core-028",
    "docs-chg-004",
    "docs-mix-043",
    "docs-chg-007",
    "docs-mix-078",
    "docs-chg-025",
    "docs-core-019",
    "docs-core-037",
    "docs-ai-020",
    "docs-ai-003-p2",
    "docs-chg-008",
    "docs-int-014",
    "docs-ai-001-p1",
    "docs-chg-022",
}
FRESH_31_60 = {
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
probes_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
store = ResultsStore(Path("results/evals.duckdb"))
try:
    v5_labels = store.labels(V5, "aleena")
    sets = {
        "new (61-90)": (V5, [x for x in v5_labels if x["case_id"] in NEW]),
        "fresh (31-60)": (V5, [x for x in v5_labels if x["case_id"] in FRESH_31_60]),
        "spot check (1-30)": (
            V5,
            [x for x in v5_labels if x["case_id"] not in NEW and x["case_id"] not in FRESH_31_60],
        ),
        "first dev run (114)": (
            "docs-20261011-223111-702e87",
            store.labels("docs-20261011-223111-702e87", "aleena"),
        ),
        "v3 champion run (80)": (
            "docs-20261016-214710-02abb7",
            store.labels("docs-20261016-214710-02abb7", "aleena"),
        ),
    }
    grades = {
        rid: (store.grades_for(rid, V2), store.grades_for(rid, V21)) for rid, _ in sets.values()
    }
finally:
    store.close()
assert len(sets["new (61-90)"][1]) == 30, len(sets["new (61-90)"][1])


def score(rid: str, labels: list[dict[str, Any]]) -> dict[str, Any]:
    g2, g21 = grades[rid]
    out: dict[str, Any] = {}
    for short, field, metric in (
        ("fully", "supported", "judge_supported"),
        ("core", "core_supported", "judge_core_supported"),
    ):
        rows = [
            (bool(x[field]), g2[k][metric] >= 1.0, g21[k][metric] >= 1.0, x["case_id"])
            for x in labels
            if x.get(field) is not None
            and metric in g2.get(k := (x["case_id"], x["attempt"]), {})
            and metric in g21.get(k, {})
        ]
        out[short] = {
            "n": len(rows),
            "agree_v2": sum(r[0] == r[1] for r in rows),
            "agree_v21": sum(r[0] == r[2] for r in rows),
            "false_pass_v2": sum((not r[0]) and r[1] for r in rows),
            "false_pass_v21": sum((not r[0]) and r[2] for r in rows),
            "judges_differ": [r[3] for r in rows if r[1] != r[2]],
        }
    return out


report: dict[str, Any] = {name: score(rid, labels) for name, (rid, labels) in sets.items()}
new = report["new (61-90)"]
c1 = (
    new["fully"]["agree_v21"] >= new["fully"]["agree_v2"]
    and new["fully"]["false_pass_v21"] <= new["fully"]["false_pass_v2"]
    and new["fully"]["false_pass_v21"] <= 2
)
c2 = new["core"]["agree_v21"] >= new["core"]["agree_v2"] - 3
earlier = ["first dev run (114)", "v3 champion run (80)", "spot check (1-30)", "fresh (31-60)"]
c3 = all(
    report[s]["fully"]["agree_v21"] >= report[s]["fully"]["agree_v2"] - 1 for s in earlier
) and (
    sum(report[s]["fully"]["agree_v21"] for s in earlier)
    >= sum(report[s]["fully"]["agree_v2"] for s in earlier)
)
c4 = bool(json.loads(probes_path.read_text(encoding="utf-8"))["criterion_3"]["holds"])
report["decision"] = {
    "criterion_1_new_fully": c1,
    "criterion_2_core_guard": c2,
    "criterion_3_earlier_fully": c3,
    "criterion_4_probes": c4,
    "identical_fully_on_new": not new["fully"]["judges_differ"],
    "adopt_v2_1": c1 and c2 and c3 and c4,
}
out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
for name in sets:
    for v in ("fully", "core"):
        r = report[name][v]
        print(
            f"{name:<22} {v:<5} n={r['n']:<3} agree v2 {r['agree_v2']:<3} v2.1 {r['agree_v21']:<3} "
            f"false pass {r['false_pass_v2']}->{r['false_pass_v21']}  differ {r['judges_differ']}"
        )
print("\nDECISION (section 10):", report["decision"])
