"""Counterfactual, on a COPY of the results store: widen the keyword lists of 10 dev cases with the
correct wordings found in BOTH runs' keyword failures, regrade both runs with the answer grader,
and re-run the ADR 0008 gate. Nothing in results/ or suites/ is modified."""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_evals.graders.deterministic import AnswerGrader
from agent_evals.harness.runner import regrade_run
from agent_evals.report import gate
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

V3, V4 = "docs-20261016-214710-02abb7", "docs-20261017-163240-f5782a"
ADD = {  # case id -> (group index, alternatives to add); each seen in a correct answer
    "docs-core-018": (1, ["automatically updates"]),
    "docs-ai-002-p2": (0, ["no separate routing charge"]),
    "docs-ai-003": (0, ["don't have to", "do not have to"]),
    "docs-chg-017": (1, ["General Purpose (SSD)"]),
    "docs-chg-025": (
        1,
        ["doesn't affect price", "doesn't change the price", "does not change the price"],
    ),
    "docs-core-011": (0, ["names are fixed", "fixed once the bucket is created"]),
    "docs-core-034": (1, ["does not recommend", "recommend against"]),
    "docs-core-037": (0, ["do not support", "manually"]),
    "docs-int-020": (0, ["no parameter charge", "cost is zero"]),
    "docs-mix-068": (0, ["until it is explicitly removed"]),
}
sim = Path(sys.argv[1])
shutil.copyfile("results/evals.duckdb", sim)
store = ResultsStore(sim)
suite = Path(store.run_info(V4)["manifest"]["suite"]["path"])
cases = load_suite(suite)
changed = []
for c in cases:
    if c.id in ADD:
        gi, alts = ADD[c.id]
        groups = [list(g) for g in c.gold.must_include]
        groups[gi] = groups[gi] + [a for a in alts if a not in groups[gi]]
        c = c.model_copy(update={"gold": c.gold.model_copy(update={"must_include": groups})})
    changed.append(c)
assert sum(c.id in ADD for c in changed) == len(ADD)


def show(title: str) -> None:
    g = gate(store, V3, V4, changed, "judge_supported", "judge")
    print(f"\n== {title}: gate {'PASSED' if g.passed else 'FAILED'}")
    for c in (g.target, *g.monitored):
        print(
            f"  {c.metric:<22} {c.baseline:.3f} -> {c.candidate:.3f}  {c.diff.mean:+.3f} "
            f"({c.diff.low:+.3f} to {c.diff.high:+.3f})  {c.verdict}"
        )
    print("  regressed categories:", g.regressed_categories or "none")


show("as recorded (original keyword lists)")
for rid in (V3, V4):
    regrade_run(store, rid, changed, [AnswerGrader()])
show("counterfactual: 10 keyword lists widened, both runs regraded")
store.close()
