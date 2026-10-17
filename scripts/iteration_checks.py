"""Post-run checks pre-registered in docs/plans/iteration-3.md, champion against challenger.

1. Answer look: median answer length of answered attempts; past a 25% change, the judge's number
   needs a 30-answer human spot check before it is trusted (ADR 0008).
2. No hedge deletion: share of answered attempts that state an absence ("does not say", "says
   nothing", ...); a fall of more than half is disclosed beside the result.
3. Mechanism, reported only: citations per answer and quote length.

Usage: uv run python -I -X utf8 scripts/iteration_checks.py CHAMPION_RUN CHALLENGER_RUN
Reads results/evals.duckdb, so no label session may be open.
"""

import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.store.db import ResultsStore

ABSENCE = re.compile(
    r"(?i)\b(?:does|do|did)\s*(?:not|n't)\s+(?:say|state|list|mention|specify|describe|document|"
    r"give|name|define|cover|include|show)\b|\bsays nothing\b|\bno mention\b|"
    r"\bnot (?:documented|mentioned|stated|listed|specified)\b|"
    r"\b(?:could|did)\s*(?:not|n't) find\b|\bI found no\b|"
    r"\bnot (?:available|included) in the (?:documentation )?snapshot\b"
)


def answered(store: ResultsStore, run_id: str) -> list[dict[str, Any]]:
    return [a["output"] for a in store.answered_attempts(run_id)]


def describe(outs: list[dict[str, Any]]) -> dict[str, float]:
    lengths = [len(str(o.get("answer", ""))) for o in outs]
    cits = [[c for c in (o.get("citations") or []) if isinstance(c, dict)] for o in outs]
    quotes = [len(str(c.get("quote", ""))) for cs in cits for c in cs]
    return {
        "answered": len(outs),
        "median_answer_chars": statistics.median(lengths),
        "absence_share": sum(bool(ABSENCE.search(str(o.get("answer", "")))) for o in outs)
        / len(outs),
        "citations_per_answer": sum(len(cs) for cs in cits) / len(outs),
        "median_quote_chars": statistics.median(quotes) if quotes else 0.0,
    }


champion, challenger = sys.argv[1], sys.argv[2]
store = ResultsStore(Path("results/evals.duckdb"))
try:
    a, b = describe(answered(store, champion)), describe(answered(store, challenger))
finally:
    store.close()
for key in a:
    print(f"{key:<22} champion {a[key]:>9.3f}   challenger {b[key]:>9.3f}")
ratio = b["median_answer_chars"] / a["median_answer_chars"]
print(
    f"\n1. answer length ratio {ratio:.2f}: "
    + (
        "past 25%, so the judge's number needs a 30-answer spot check before it is trusted"
        if abs(ratio - 1) > 0.25
        else "within 25%, no spot check required"
    )
)
fell = a["absence_share"] > 0 and b["absence_share"] < a["absence_share"] / 2
print(
    f"2. absence statements {a['absence_share']:.3f} -> {b['absence_share']:.3f}: "
    + ("fell by more than half; disclose beside the result" if fell else "did not fall by half")
)
