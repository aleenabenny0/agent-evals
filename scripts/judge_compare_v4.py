"""Score judge v2 (grader "judge") and candidate judge v4 (grader "judge-v4") against the 40 labels
made for that comparison on the champion dev run, apply the decision rule committed in
docs/judge-validation.md section 8, and package every answer where labeler and judges differ.

Usage: uv run python -I -X utf8 scripts/judge_compare_v4.py OUT_DIR
Writes OUT_DIR/scores.json and OUT_DIR/disputed-answers.json. Reads results/evals.duckdb (so no
label session may be open), both judges' probe reports for the run in results/, and, through git,
the label export of commit 2385965: it fixes the 40 labels that section 7 scored, which this
comparison must leave out.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, "src")
from agent_evals.graders.judge_prompts import JUDGE_RESPONSIVE_V1, SUPPORT_RUBRICS
from agent_evals.judge_probes import MUTANTS, report_path
from agent_evals.judge_validation import agreement
from agent_evals.stats.core import cohens_kappa, mcnemar_exact, wilson_ci
from agent_evals.stats.power import cases_for_paired_delta
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

RUN = "docs-20261016-214710-02abb7"
LABELER = "aleena"
SECTION7_EXPORT = "2385965"  # the commit whose label export holds the 40 labels of section 7
V4_VERSION = "bf1a8e9f376a"  # the judge version section 8 names
KAPPA_BAR = 0.7
MUTANT_BAR = 0.90
VERDICTS = [
    ("core_supported", "judge_core_supported"),
    ("supported", "judge_supported"),
    ("responsive", "judge_responsive"),
]
NAMES = {
    "core_supported": "core supported",
    "supported": "fully supported",
    "responsive": "responsive",
}
out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)

old_export = subprocess.run(
    ["git", "show", f"{SECTION7_EXPORT}:results/labels/{RUN}.jsonl"],
    check=True,
    capture_output=True,
    text=True,
    encoding="utf-8",
).stdout
section7 = {json.loads(line)["case_id"] for line in old_export.splitlines() if line.strip()}
assert len(section7) == 40, len(section7)

store = ResultsStore(Path("results/evals.duckdb"))
info = store.run_info(RUN)
all_labels = store.labels(RUN, LABELER)
answered = store.answered_attempts(RUN)
v2 = store.grade_reasons(RUN, "judge")
v4 = store.grade_reasons(RUN, "judge-v4")
outs = {(a["case_id"], a["attempt"]): a["output"] for a in store.all_attempts(RUN)}
store.close()
cases = {c.id: c for c in load_suite(Path(info["manifest"]["suite"]["path"]))}

# The label set the rule names: the next 40 answered attempt-1 answers in case-id order.
labels = [x for x in all_labels if x["case_id"] not in section7]
expected = [
    (a["case_id"], a["attempt"])
    for a in answered
    if a["attempt"] == 1 and a["case_id"] in cases and a["case_id"] not in section7
][:40]
assert [(x["case_id"], x["attempt"]) for x in labels] == expected, "not the set section 8 names"
v4_info = info["manifest"].get("judge-v4", {})
assert v4_info.get("version") == V4_VERSION and v4_info.get("rubric") == "v4", v4_info
missing = [k for k in expected if k not in v4 or k not in v2]
assert not missing, f"answers without both judges' grades: {missing}"


def kappa_ci(h: list[bool], j: list[bool], seed: int = 0, n_boot: int = 10_000) -> list[float]:
    rng = np.random.default_rng(seed)
    n = len(h)
    ks = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        ks.append(cohens_kappa([h[i] for i in idx], [j[i] for i in idx]))
    lo, hi = np.quantile(ks, [0.025, 0.975])
    return [float(lo), float(hi)]


def delta_kappa_ci(
    h: list[bool], a: list[bool], b: list[bool], seed: int = 1, n_boot: int = 10_000
) -> list[float]:
    rng = np.random.default_rng(seed)
    n = len(h)
    ds = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        hh = [h[i] for i in idx]
        ds.append(cohens_kappa(hh, [b[i] for i in idx]) - cohens_kappa(hh, [a[i] for i in idx]))
    lo, hi = np.quantile(ds, [0.025, 0.975])
    return [float(lo), float(hi)]


def passed(grades: dict[str, Any], metric: str) -> bool:
    return bool(grades[metric][0] >= 1.0)


report: dict[str, Any] = {
    "run": RUN,
    "labeler": LABELER,
    "n_labels": len(labels),
    "cases": f"{labels[0]['case_id']} to {labels[-1]['case_id']}",
    "judge_versions": {"v2": info["manifest"]["judge"]["version"], "v4": v4_info["version"]},
    "verdicts": {},
}
for field, metric in VERDICTS:
    rows = [
        (x, bool(x[field]), passed(v2[k], metric), passed(v4[k], metric))
        for x in labels
        if x.get(field) is not None
        and metric in v2[(k := (x["case_id"], x["attempt"]))]
        and metric in v4[k]
    ]
    h = [r[1] for r in rows]
    j2 = [r[2] for r in rows]
    j4 = [r[3] for r in rows]
    entry: dict[str, Any] = {"n": len(rows), "human_fail": sum(1 for v in h if not v)}
    for name, j in (("v2", j2), ("v4", j4)):
        a = agreement(field, list(zip(h, j, strict=True)))
        agree = sum(1 for x, y in zip(h, j, strict=True) if x == y)
        aw = wilson_ci(agree, len(h))
        entry[name] = {
            "kappa": a.kappa,
            "kappa_ci": kappa_ci(h, j),
            "agree": agree,
            "agree_ci": [aw.low, aw.high],
            "tpr": None if a.tpr is None else [a.tpr.mean, a.tpr.low, a.tpr.high],
            "tnr": None if a.tnr is None else [a.tnr.mean, a.tnr.low, a.tnr.high],
            "tp_fp_fn_tn": [a.tp, a.fp, a.fn, a.tn],
            "judge_pass_rate": sum(j) / len(j),
        }
    b = sum(1 for x, p, q in zip(h, j2, j4, strict=True) if p == x and q != x)  # only v2 agrees
    c = sum(1 for x, p, q in zip(h, j2, j4, strict=True) if q == x and p != x)  # only v4 agrees
    entry["only_v2_agrees"] = b
    entry["only_v4_agrees"] = c
    entry["mcnemar_p"] = mcnemar_exact(b, c)
    entry["delta_kappa_v4_minus_v2"] = entry["v4"]["kappa"] - entry["v2"]["kappa"]
    entry["delta_kappa_ci"] = delta_kappa_ci(h, j2, j4)
    entry["judges_disagree_with_each_other"] = sum(1 for p, q in zip(j2, j4, strict=True) if p != q)
    if c > b:
        d, delta = (b + c) / len(h), (c - b) / len(h)
        entry["labels_needed_to_detect_observed_gain"] = cases_for_paired_delta(delta, d)
    subsets = {}
    for side in ("judge-dev", "judge-test"):
        sub = [r for r in rows if r[0]["label_split"] == side]
        if sub:
            sh = [r[1] for r in sub]
            subsets[side] = {
                "n": len(sub),
                "human_fail": sum(1 for v in sh if not v),
                "v2_agree": sum(1 for r in sub if r[2] == r[1]),
                "v4_agree": sum(1 for r in sub if r[3] == r[1]),
            }
    entry["by_label_side"] = subsets
    report["verdicts"][field] = entry

# Probes (rule 3): each judge's own report on this run.
probe_reports = {
    name: json.loads(report_path(RUN, grader).read_text(encoding="utf-8"))
    for name, grader in (("v2", "judge"), ("v4", "judge-v4"))
}
assert probe_reports["v4"]["judge"] == V4_VERSION, probe_reports["v4"]["judge"]
assert probe_reports["v2"]["judge"] == report["judge_versions"]["v2"]
report["probes"] = {name: p["summary"] for name, p in probe_reports.items()}
v4_probes = probe_reports["v4"]["summary"]
rule3_missing = [m for m in MUTANTS if m not in v4_probes or not v4_probes[m]["n"]]

# Decision rule, section 8.
core, full = report["verdicts"]["core_supported"], report["verdicts"]["supported"]
rule1 = (
    full["v4"]["kappa"] >= KAPPA_BAR
    and full["v4"]["agree"] >= full["v2"]["agree"]
    and not (full["only_v2_agrees"] > full["only_v4_agrees"] and full["mcnemar_p"] < 0.05)
)
rule2 = core["v4"]["agree"] >= core["v2"]["agree"] - 1
rule3 = not rule3_missing and all(v4_probes[m]["rate"] >= MUTANT_BAR for m in MUTANTS)
report["decision"] = {
    "rule1_fully_supported": rule1,
    "rule2_core_supported": rule2,
    "rule3_probes": rule3,
    "rule3_mutant_kinds_without_data": rule3_missing,
    "v4_replaces_v2": rule1 and rule2 and rule3,
}

# Evidence packages: every answer where the three raters differ on any verdict.
packages = []
yn = {True: "yes", False: "no", None: "not asked"}


def pf(v: float) -> str:
    return "pass" if v >= 1.0 else "fail"


for x in labels:
    k = (x["case_id"], x["attempt"])
    disputed = [
        field
        for field, metric in VERDICTS
        if x.get(field) is not None
        and len({bool(x[field]), passed(v2[k], metric), passed(v4[k], metric)}) > 1
    ]
    if not disputed:
        continue
    case = cases[k[0]]
    out = outs[k]
    cits = [cc for cc in (out.get("citations") or []) if isinstance(cc, dict)]
    packages.append(
        {
            "id": f"{k[0]}#{k[1]}",
            "case_id": k[0],
            "attempt": k[1],
            "type": case.type,
            "area": case.area,
            "label_side": x["label_split"],
            "question": case.question,
            "reference_answer": case.gold.answer,
            "answer": str(out.get("answer", "")),
            "citations": [
                {"n": i, "url": str(cc.get("url", "")), "quote": str(cc.get("quote", ""))}
                for i, cc in enumerate(cits, 1)
            ],
            "disputed": ["fully_supported" if f == "supported" else f for f in disputed],
            "human": {
                "core_supported": yn[x.get("core_supported")],
                "fully_supported": yn[x.get("supported")],
                "responsive": yn[x.get("responsive")],
                "note": x.get("note") or "",
            },
            **{
                f"judge_{name}": {
                    "core_supported": pf(g[k]["judge_core_supported"][0]),
                    "fully_supported": pf(g[k]["judge_supported"][0]),
                    "responsive": pf(g[k]["judge_responsive"][0]),
                    "support_reason": g[k]["judge_supported"][1],
                    "responsive_reason": g[k]["judge_responsive"][1],
                }
                for name, g in (("v2", v2), ("v4", v4))
            },
        }
    )

(out_dir / "scores.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
(out_dir / "disputed-answers.json").write_text(
    json.dumps(
        {
            "rubric_v2": SUPPORT_RUBRICS["v2"],
            "rubric_v4": SUPPORT_RUBRICS["v4"],
            "responsive_prompt": JUDGE_RESPONSIVE_V1,
            "items": packages,
        },
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)


def interval(est: list[float] | None) -> str:
    return "-" if est is None else f"{est[0]:.2f} ({est[1]:.2f} to {est[2]:.2f})"


print(f"{report['n_labels']} labels, {report['cases']}; judges {report['judge_versions']}")
for field, _ in VERDICTS:
    e = report["verdicts"][field]
    differ = e["judges_disagree_with_each_other"]
    head = f"\n== {NAMES[field]}: n={e['n']}, labeler fails {e['human_fail']}"
    print(f"{head}, judges differ on {differ}")
    for name in ("v2", "v4"):
        j = e[name]
        agree = f"{j['agree']}/{e['n']} ({j['agree_ci'][0]:.2f} to {j['agree_ci'][1]:.2f})"
        kappa = f"{j['kappa']:.3f} ({j['kappa_ci'][0]:.2f} to {j['kappa_ci'][1]:.2f})"
        counts = "/".join(map(str, j["tp_fp_fn_tn"]))
        print(f"   {name}: agree {agree}; kappa {kappa}")
        print(f"       TPR {interval(j['tpr'])}; TNR {interval(j['tnr'])}; tp/fp/fn/tn {counts}")
    lo, hi = e["delta_kappa_ci"]
    print(
        f"   paired: only v2 agrees {e['only_v2_agrees']}, only v4 agrees {e['only_v4_agrees']}, "
        f"McNemar p = {e['mcnemar_p']:.3g}; kappa v4-v2 {e['delta_kappa_v4_minus_v2']:+.3f} "
        f"({lo:+.2f} to {hi:+.2f})"
    )
    if "labels_needed_to_detect_observed_gain" in e:
        needed = e["labels_needed_to_detect_observed_gain"]
        print(f"   labels needed to detect the observed v4 gain at 80% power: {needed}")
    for side, s in e["by_label_side"].items():
        print(
            f"   [{side}] n={s['n']} fails={s['human_fail']}: "
            f"v2 agree {s['v2_agree']}, v4 agree {s['v4_agree']}"
        )
print("\n== probes on the champion run (mutants: share caught; probes: share unchanged)")
for probe in [*MUTANTS, "padding", "resample"]:
    cells = []
    for name in ("v2", "v4"):
        s = report["probes"][name].get(probe)
        if s is None:
            cells.append(f"{name} -")
        else:
            ci = f"{s['low']:.2f}-{s['high']:.2f}"
            cells.append(f"{name} {s['rate']:.2f} ({ci}, n={s['n']})")
    print(f"   {probe:<9} " + "   ".join(cells))
print(f"\nDECISION (rule committed in 050f2f2, version fixed in 51c7aa7): {report['decision']}")
ids = [p["id"] + ":" + ",".join(p["disputed"]) for p in packages]
print(f"disputed answers packaged: {len(packages)} -> {ids}")
