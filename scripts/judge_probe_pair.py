"""Paired judge probes for docs/judge-validation.md section 9, criterion 3: both judges grade the
SAME variants, built from the answers both judges pass on both verdicts (the first 40 in case-id
order). Also a harmless-attribution probe, reported only: each passed answer prefixed with
"According to the documentation: ", whose verdicts should not change. Costs money (judge calls).

Usage: uv run python -I -X utf8 scripts/judge_probe_pair.py RUN CONFIG_A CONFIG_B OUT.json
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from dotenv import load_dotenv

from agent_evals.config import load_config
from agent_evals.graders.judge import JudgeGrader
from agent_evals.harness.cache import CallCache
from agent_evals.harness.llm import AnthropicClient
from agent_evals.harness.runner import grade_all
from agent_evals.judge_probes import MUTANTS, Variant, behaved, variants
from agent_evals.stats.core import wilson_ci
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite
from agent_evals.types import AgentResult

FRAME = "According to the documentation: "
run_id, cfg_a_path, cfg_b_path, out_path = sys.argv[1:5]
load_dotenv()
cfg_a, cfg_b = load_config(Path(cfg_a_path)), load_config(Path(cfg_b_path))
names = (cfg_a.judge.grader_name, cfg_b.judge.grader_name)
store = ResultsStore(cfg_a.paths.db)
try:
    info = store.run_info(run_id)
    cases = {c.id: c for c in load_suite(Path(str(info["manifest"]["suite"]["path"])))}
    verdicts = {n: store.grades_for(run_id, n) for n in names}
    answered = store.answered_attempts(run_id)
finally:
    store.close()


def passes_both(v: dict[str, float]) -> bool:
    return v.get("judge_supported") == 1.0 and v.get("judge_responsive") == 1.0


base = [
    a
    for a in answered
    if a["case_id"] in cases
    and all(passes_both(verdicts[n].get((a["case_id"], a["attempt"]), {})) for n in names)
][:40]
pool = [
    (str(c.get("url", "")), str(c.get("quote", "")))
    for a in answered
    for c in (a["output"].get("citations") or [])
    if isinstance(c, dict)
]
llm = AnthropicClient(CallCache(cfg_a.paths.cache_dir))
graders = {
    (n, salt): JudgeGrader(llm, cfg.judge, salt=salt)
    for n, cfg in zip(names, (cfg_a, cfg_b), strict=True)
    for salt in ("judge", "probe")
}
jobs: list[tuple[dict[str, Any], Variant]] = [
    (a, v) for a in base for v in variants(a["output"], pool)
]
for a in base:
    out = dict(a["output"])
    out["answer"] = FRAME + str(out.get("answer", ""))
    jobs.append((a, Variant(probe="attribution", kind="probe", output=out, salt="judge")))


def one(job: tuple[dict[str, Any], Variant]) -> tuple[str, dict[str, bool]]:
    a, v = job
    case = cases[a["case_id"]]
    ok: dict[str, bool] = {}
    for n in names:
        graded = grade_all([graders[(n, v.salt)]], case, AgentResult(output=v.output))
        judged = {g.metric: g.value for _, g in graded if g.metric != "grader_error"}
        original = verdicts[n][(a["case_id"], a["attempt"])]
        if v.probe == "attribution":  # a harmless frame: verdicts should not change
            ok[n] = all(
                judged.get(m) == original.get(m) for m in ("judge_supported", "judge_responsive")
            )
        else:
            ok[n] = behaved(v, original, judged)
    return v.probe, ok


tally: dict[str, dict[str, list[bool]]] = {}
with ThreadPoolExecutor(max_workers=cfg_a.run.workers) as ex:
    for probe, ok in ex.map(one, jobs):
        for n in names:
            tally.setdefault(probe, {}).setdefault(n, []).append(ok[n])
summary: dict[str, Any] = {
    "run": run_id,
    "judges": list(names),
    "base_answers": len(base),
    "probes": {},
}
for probe, by in tally.items():
    summary["probes"][probe] = {
        n: {"ok": sum(v), "n": len(v), "rate": wilson_ci(sum(v), len(v)).mean}
        for n, v in by.items()
    }
a_name, b_name = names
crit = {
    m: summary["probes"][m][b_name]["ok"] >= summary["probes"][m][a_name]["ok"] - 1
    for m in MUTANTS
    if m in summary["probes"]
}
summary["criterion_3"] = {
    "per_mutant": crit,
    "holds": bool(crit) and all(crit.values()) and len(crit) == len(MUTANTS),
}
Path(out_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(f"base answers both judges pass: {len(base)}; variants: {len(jobs)}")
for probe, by in summary["probes"].items():
    cells = "   ".join(f"{n} {s['ok']}/{s['n']}" for n, s in by.items())
    print(f"  {probe:<12} {cells}")
print("criterion 3:", summary["criterion_3"])
