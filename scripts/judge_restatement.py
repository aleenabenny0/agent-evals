"""Restate every reported run's judge metrics under two judges side by side, and the paired
comparisons the CHANGELOG reports, for docs/judge-validation.md section 9 step 4.

Usage: uv run python -I -X utf8 scripts/judge_restatement.py OLD_JUDGE NEW_JUDGE OUT.json
(grader names, for example judge judge-v2.1). Reads results/evals.duckdb.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.report import compare, summarize
from agent_evals.store.db import ResultsStore

RUNS = {
    "dev: docs v2 (baseline)": "docs-20261011-223111-702e87",
    "dev: docs v3 (champion)": "docs-20261016-214710-02abb7",
    "dev: iteration 2 challenger": "docs-20261016-225705-5447b9",
    "dev: iteration 3 challenger (v4)": "docs-20261017-163240-f5782a",
    "dev: iteration 4 challenger (v5)": "docs-20261017-174739-1c0e8e",
    "dev: oracle baseline": "oracle-evidence-20261011-225523-da3dc1",
    "test: docs v2 (baseline)": "docs-20261011-225715-b67388",
    "test: docs v3": "docs-20261016-222256-04f0f4",
}
PAIRS = {
    "iteration 1 dev (v2 -> v3)": ("docs-20261011-223111-702e87", "docs-20261016-214710-02abb7"),
    "iteration 1 test (v2 -> v3)": ("docs-20261011-225715-b67388", "docs-20261016-222256-04f0f4"),
    "iteration 2 dev (v3 -> challenger)": (
        "docs-20261016-214710-02abb7",
        "docs-20261016-225705-5447b9",
    ),
    "iteration 3 dev (v3 -> v4)": ("docs-20261016-214710-02abb7", "docs-20261017-163240-f5782a"),
    "iteration 4 dev (v3 -> v5)": ("docs-20261016-214710-02abb7", "docs-20261017-174739-1c0e8e"),
}
METRICS = ("judge_supported", "judge_core_supported", "judge_responsive")
old, new, out_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
store = ResultsStore(Path("results/evals.duckdb"))
out: dict[str, Any] = {"judges": [old, new], "runs": {}, "pairs": {}}
try:
    for name, rid in RUNS.items():
        out["runs"][name] = {}
        for judge in (old, new):
            m = summarize(store, rid, judge).metrics
            out["runs"][name][judge] = {
                k: [m[k].mean, m[k].low, m[k].high, m[k].n] for k in METRICS if k in m
            }
    for name, (a, b) in PAIRS.items():
        out["pairs"][name] = {}
        for judge in (old, new):
            c = compare(store, a, b, "judge_supported", judge)
            out["pairs"][name][judge] = {
                "baseline": c.baseline,
                "candidate": c.candidate,
                "diff": [c.diff.mean, c.diff.low, c.diff.high],
                "p": c.p_value,
                "n": c.n_cases,
                "verdict": c.verdict,
            }
finally:
    store.close()
out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
for name, by in out["runs"].items():
    cells = []
    for judge in (old, new):
        s = by[judge].get("judge_supported")
        cells.append(f"{judge} {s[0]:.3f}" if s else f"{judge} -")
    print(f"{name:<36} judge_supported  " + "   ".join(cells))
for name, by in out["pairs"].items():
    cells = []
    for judge in (old, new):
        p = by[judge]
        d = p["diff"]
        cells.append(
            f"{judge} {p['baseline']:.3f}->{p['candidate']:.3f} {d[0]:+.3f} "
            f"({d[1]:+.3f} to {d[2]:+.3f})"
        )
    print(f"{name:<36} " + "   ".join(cells))
