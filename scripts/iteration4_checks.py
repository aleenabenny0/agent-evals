"""Post-run checks pre-registered in docs/plans/iteration-4.md (v3 champion, v4, v5 challenger).

1. Answer look: median answer length, v5 against v3 (past -25%: spot check before trusting).
2. Errors: errored attempts, v5 against v3 (more than 3 extra: disclose).
3. Missed abstention, case by case: unanswerable cases v5 answers in 2+ of 3 attempts where v3
   abstained in 2+ of 3 (disclose each).
4. Attribution, diagnostic: for cases where v5 has fewer wrong abstentions than v4, whether v5
   read a first search result that v4 never read in that case (rule 1) or not (rule 2).
5. Absence statements: share of answered attempts stating an absence, v3 / v4 / v5.
Also prints the pre-registered 30-answer spot-check sample and its label command.

Usage: uv run python -I -X utf8 scripts/iteration4_checks.py V3_RUN V4_RUN V5_RUN
"""

import hashlib
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

ABSENCE = re.compile(
    r"(?i)\b(?:does|do|did)\s*(?:not|n't)\s+(?:say|state|list|mention|specify|describe|document|"
    r"give|name|define|cover|include|show)\b|\bsays nothing\b|\bno mention\b|"
    r"\bnot (?:documented|mentioned|stated|listed|specified)\b|"
    r"\b(?:could|did)\s*(?:not|n't) find\b|\bI found no\b|"
    r"\bnot (?:available|included) in the (?:documentation )?snapshot\b"
)
URL = re.compile(r"URL: (\S+)")
SALT = "iteration-4-spot-check:"

v3_id, v4_id, v5_id = sys.argv[1:4]
store = ResultsStore(Path("results/evals.duckdb"))
try:
    att = {rid: store.all_attempts(rid) for rid in (v3_id, v4_id, v5_id)}
    wrong_abst = {
        rid: store.metric_by_case(rid, "wrong_abstention") for rid in (v3_id, v4_id, v5_id)
    }
    suite = Path(store.run_info(v5_id)["manifest"]["suite"]["path"])
finally:
    store.close()
cases = {c.id: c for c in load_suite(suite)}


def answered(rid: str) -> list[dict[str, Any]]:
    return [a for a in att[rid] if not a["error"] and not a["output"].get("abstained")]


def median_len(rid: str) -> float:
    return statistics.median(len(str(a["output"].get("answer", ""))) for a in answered(rid))


# 1. answer look
ratio = median_len(v5_id) / median_len(v3_id)
print(
    f"1. median answer length v3 {median_len(v3_id):.0f}, v5 {median_len(v5_id):.0f}, "
    f"ratio {ratio:.2f}: "
    + (
        "spot check required before the number is trusted"
        if ratio < 0.75 or ratio > 1.25
        else "no spot check required"
    )
)

# 2. errors
errs = {
    rid: [f"{a['case_id']}#{a['attempt']}: {str(a['error'])[:40]}" for a in att[rid] if a["error"]]
    for rid in (v3_id, v5_id)
}
extra = len(errs[v5_id]) - len(errs[v3_id])
print(
    f"2. errored attempts v3 {len(errs[v3_id])}, v5 {len(errs[v5_id])}"
    + (f" -> {extra} extra: disclose" if extra > 3 else " (within 3)")
    + f"; v5: {errs[v5_id]}"
)


# 3. missed abstention, case by case
def count_attempts(rid: str, cid: str, abstained: bool) -> int:
    return sum(
        1
        for a in att[rid]
        if a["case_id"] == cid
        and not a["error"]
        and bool(a["output"].get("abstained")) == abstained
    )


flagged = []
for cid, c in sorted(cases.items()):
    if c.type != "unanswerable":
        continue
    if count_attempts(v5_id, cid, False) >= 2 and count_attempts(v3_id, cid, True) >= 2:
        flagged.append(cid)
    counts = ", ".join(
        f"{name} {count_attempts(rid, cid, False)}/3"
        for name, rid in (("v3", v3_id), ("v4", v4_id), ("v5", v5_id))
    )
    print(f"   {cid}: answered {counts}")
print(f"3. unanswerable cases v5 answers where v3 abstained: {flagged or 'none'}")


# 4. attribution of fixed abstentions
def reads(rid: str, cid: str) -> set[str]:
    return {
        str((s.get("input") or {}).get("url", ""))
        for a in att[rid]
        if a["case_id"] == cid
        for s in a["trajectory"]
        if s.get("kind") == "tool" and s.get("name") == "read_page"
    }


def first_results(a: dict[str, Any]) -> set[str]:
    out = set()
    for s in a["trajectory"]:
        if s.get("kind") == "tool" and s.get("name") == "search_docs":
            m = URL.search(str(s.get("output", "")))
            if m:
                out.add(m.group(1))
    return out


print("4. cases where v5 has fewer wrong abstentions than v4:")
for cid in sorted(wrong_abst[v4_id]):
    w4 = sum(wrong_abst[v4_id].get(cid, []))
    w5 = sum(wrong_abst[v5_id].get(cid, []))
    if w5 < w4:
        new_first = {
            u
            for a in att[v5_id]
            if a["case_id"] == cid
            for u in first_results(a)
            if u in reads(v5_id, cid)
        } - reads(v4_id, cid)
        rule = "rule 1 (read a new first result)" if new_first else "rule 2 (same evidence, flag)"
        print(f"   {cid}: wrong abstentions v4 {w4:.0f} -> v5 {w5:.0f}; {rule} {sorted(new_first)}")

# 5. absence statements
for rid, name in ((v3_id, "v3"), (v4_id, "v4"), (v5_id, "v5")):
    ans = answered(rid)
    share = sum(bool(ABSENCE.search(str(a["output"].get("answer", "")))) for a in ans) / len(ans)
    print(f"5. absence statements {name}: {share:.3f} of {len(ans)} answered attempts")

# The pre-registered spot-check sample.
pool = sorted(
    (a for a in answered(v5_id) if a["attempt"] == 1),
    key=lambda a: hashlib.sha256((SALT + a["case_id"]).encode()).hexdigest(),
)[:30]
ids = [a["case_id"] for a in pool]
print(f"\nspot-check sample ({len(ids)}): {ids}")
print(
    "label command:\n  uv run evals label "
    + v5_id
    + " --labeler aleena "
    + " ".join(f"--case {c}" for c in ids)
)
