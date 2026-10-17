"""Snapshot every dev run's summary and the README's paired comparisons, so a regrade's effect is on
record. Usage: uv run python -I -X utf8 scripts/dev_runs_snapshot.py OUT.json
Reads results/evals.duckdb (no label session may be open). Judge metrics: the validated judge."""

import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_evals.report import compare, summarize
from agent_evals.store.db import ResultsStore

DEV_RUNS = {
    "docs v2 (baseline)": "docs-20261011-223111-702e87",
    "closed-book": "closed-book-20261011-225242-b58341",
    "oracle": "oracle-evidence-20261011-225523-da3dc1",
    "docs v3 (champion)": "docs-20261016-214710-02abb7",
    "iteration 2 challenger": "docs-20261016-225705-5447b9",
    "iteration 3 challenger (v4)": "docs-20261017-163240-f5782a",
}
COMPARISONS = [
    (
        "closed-book minus docs v2 (retrieval)",
        "docs-20261011-223111-702e87",
        "closed-book-20261011-225242-b58341",
    ),
    (
        "oracle minus docs v2",
        "docs-20261011-223111-702e87",
        "oracle-evidence-20261011-225523-da3dc1",
    ),
    ("iteration 1: v3 minus v2", "docs-20261011-223111-702e87", "docs-20261016-214710-02abb7"),
    (
        "iteration 2: challenger minus v3",
        "docs-20261016-214710-02abb7",
        "docs-20261016-225705-5447b9",
    ),
    ("iteration 3: v4 minus v3", "docs-20261016-214710-02abb7", "docs-20261017-163240-f5782a"),
]
store = ResultsStore(Path("results/evals.duckdb"))
try:
    out: dict = {"runs": {}, "comparisons": {}}
    for name, rid in DEV_RUNS.items():
        s = summarize(store, rid)
        out["runs"][name] = {
            "run_id": rid,
            "metrics": {k: [v.mean, v.low, v.high, v.n] for k, v in s.metrics.items()},
            "flip_rate": None
            if s.flip_rate is None
            else [s.flip_rate.mean, s.flip_rate.low, s.flip_rate.high],
            "pass_hat_k": s.pass_hat_k,
            "pass_at_k": s.pass_at_k,
        }
    for name, a, b in COMPARISONS:
        c = compare(store, a, b, "correct")
        out["comparisons"][name] = {
            "baseline": c.baseline,
            "candidate": c.candidate,
            "diff": [c.diff.mean, c.diff.low, c.diff.high],
            "p": c.p_value,
            "n": c.n_cases,
            "verdict": c.verdict,
        }
finally:
    store.close()
Path(sys.argv[1]).write_text(json.dumps(out, indent=1), encoding="utf-8")
for name, r in out["runs"].items():
    print(f"{name:<28} correct {r['metrics']['correct'][0]:.3f}  flip {r['flip_rate']}")
for name, c in out["comparisons"].items():
    d = c["diff"]
    print(
        f"{name:<40} {c['baseline']:.3f} -> {c['candidate']:.3f}  {d[0]:+.3f} "
        f"({d[1]:+.3f} to {d[2]:+.3f}) p={c['p']:.2g}"
    )
