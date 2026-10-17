"""Score judge v2 (grader "judge") and candidate judge v3 (grader "judge-v3") against the 40 fresh
labels on the champion dev run, apply the decision rule committed in docs/judge-validation.md
section 6, and write evidence packages for every answer where labeler and judges disagree.

Usage: uv run python -I -X utf8 scripts/judge_compare.py RESULTS_JSON PACKAGES_JSON
Run on 2026-10-16; its outputs are results/judge-v2-vs-v3/scores.json and
disputed-answers.json. Reads results/evals.duckdb, so no label session may be open.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")
from agent_evals.graders.judge_prompts import JUDGE_RESPONSIVE_V1, SUPPORT_RUBRICS
from agent_evals.judge_validation import agreement
from agent_evals.stats.core import cohens_kappa, mcnemar_exact, wilson_ci
from agent_evals.stats.power import cases_for_paired_delta
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

RUN = "docs-20261016-214710-02abb7"
LABELER = "aleena"
V3_INFORMED = {
    "docs-ai-004-p3",
    "docs-ai-007",
    "docs-ai-023",
    "docs-core-001",
    "docs-core-025",
    "docs-mix-003",
    "docs-mix-057",
    "docs-mix-078",
}
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
results_path, packages_path = Path(sys.argv[1]), Path(sys.argv[2])

store = ResultsStore(Path("results/evals.duckdb"))
labels = store.labels(RUN, LABELER)
v2 = store.grade_reasons(RUN, "judge")
v3 = store.grade_reasons(RUN, "judge-v3")
outs = {(a["case_id"], a["attempt"]): a["output"] for a in store.all_attempts(RUN)}
suite_path = Path(store.run_info(RUN)["manifest"]["suite"]["path"])
store.close()
cases = {c.id: c for c in load_suite(suite_path)}
assert len(labels) == 40, len(labels)


def kappa_ci(
    h: list[bool], j: list[bool], seed: int = 0, n_boot: int = 10_000
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(h)
    ks = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        ks.append(cohens_kappa([h[i] for i in idx], [j[i] for i in idx]))
    lo, hi = np.quantile(ks, [0.025, 0.975])
    return float(lo), float(hi)


def delta_kappa_ci(h, a, b, seed: int = 1, n_boot: int = 10_000) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(h)
    ds = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        hh = [h[i] for i in idx]
        ds.append(cohens_kappa(hh, [b[i] for i in idx]) - cohens_kappa(hh, [a[i] for i in idx]))
    lo, hi = np.quantile(ds, [0.025, 0.975])
    return float(lo), float(hi)


report: dict = {"run": RUN, "labeler": LABELER, "n_labels": len(labels), "verdicts": {}}
rows_by_verdict: dict[str, list] = {}
for field, metric in VERDICTS:
    rows = []
    for x in labels:
        k = (x["case_id"], x["attempt"])
        if x.get(field) is None or metric not in v2.get(k, {}) or metric not in v3.get(k, {}):
            continue
        rows.append((x, bool(x[field]), v2[k][metric][0] >= 1.0, v3[k][metric][0] >= 1.0))
    rows_by_verdict[field] = rows
    h = [r[1] for r in rows]
    j2 = [r[2] for r in rows]
    j3 = [r[3] for r in rows]
    entry: dict = {"n": len(rows), "human_fail": sum(1 for v in h if not v)}
    for name, j in (("v2", j2), ("v3", j3)):
        a = agreement(field, list(zip(h, j, strict=True)))
        agree = sum(1 for x, y in zip(h, j, strict=True) if x == y)
        lo, hi = kappa_ci(h, j)
        aw = wilson_ci(agree, len(h))
        entry[name] = {
            "kappa": a.kappa,
            "kappa_ci": [lo, hi],
            "agree": agree,
            "agree_ci": [aw.low, aw.high],
            "tpr": None if a.tpr is None else [a.tpr.mean, a.tpr.low, a.tpr.high],
            "tnr": None if a.tnr is None else [a.tnr.mean, a.tnr.low, a.tnr.high],
            "tp_fp_fn_tn": [a.tp, a.fp, a.fn, a.tn],
            "judge_pass_rate": sum(j) / len(j),
        }
    b = sum(1 for x, p, q in zip(h, j2, j3, strict=True) if p == x and q != x)  # only v2 agrees
    c = sum(1 for x, p, q in zip(h, j2, j3, strict=True) if q == x and p != x)  # only v3 agrees
    entry["only_v2_agrees"] = b
    entry["only_v3_agrees"] = c
    entry["mcnemar_p"] = mcnemar_exact(b, c)
    entry["delta_kappa_v3_minus_v2"] = entry["v3"]["kappa"] - entry["v2"]["kappa"]
    entry["delta_kappa_ci"] = list(delta_kappa_ci(h, j2, j3))
    entry["judges_disagree_with_each_other"] = sum(1 for p, q in zip(j2, j3, strict=True) if p != q)
    if c > b:
        d, delta = (b + c) / len(h), (c - b) / len(h)
        entry["labels_needed_to_detect_observed_gain"] = cases_for_paired_delta(delta, d)
    # subsets, fixed in advance by the decision rule
    subsets = {}
    for sname, pred in (
        (
            "judge-test side, not v3-informed",
            lambda x: x["label_split"] == "judge-test" and x["case_id"] not in V3_INFORMED,
        ),
        (
            "judge-test side, v3-informed",
            lambda x: x["label_split"] == "judge-test" and x["case_id"] in V3_INFORMED,
        ),
        ("judge-dev side", lambda x: x["label_split"] == "judge-dev"),
    ):
        sub = [r for r in rows if pred(r[0])]
        if not sub:
            continue
        sh = [r[1] for r in sub]
        subsets[sname] = {
            "n": len(sub),
            "human_fail": sum(1 for v in sh if not v),
            "v2_agree": sum(1 for r in sub if r[2] == r[1]),
            "v3_agree": sum(1 for r in sub if r[3] == r[1]),
            "v2_kappa": cohens_kappa(sh, [r[2] for r in sub]),
            "v3_kappa": cohens_kappa(sh, [r[3] for r in sub]),
        }
    entry["subsets"] = subsets
    report["verdicts"][field] = entry

# decision rule, section 6
core, full = report["verdicts"]["core_supported"], report["verdicts"]["supported"]
rule1 = core["only_v3_agrees"] > core["only_v2_agrees"] and core["mcnemar_p"] < 0.05
rule2 = (full["v3"]["agree"] >= full["v2"]["agree"] - 1) and not (
    full["only_v2_agrees"] > full["only_v3_agrees"] and full["mcnemar_p"] < 0.05
)
report["decision"] = {
    "rule1_core_v3_better_beyond_chance": rule1,
    "rule2_fully_not_worse": rule2,
    "adopt_v3": rule1 and rule2,
}

# evidence packages for every answer where any of the three raters differ on any verdict
packages = []
by_key = {(x["case_id"], x["attempt"]): x for x in labels}
yn = {True: "yes", False: "no", None: "not asked"}
pf = lambda v: "pass" if v >= 1.0 else "fail"  # noqa: E731
for k, x in sorted(by_key.items()):
    disputed = []
    for field, metric in VERDICTS:
        hv = x.get(field)
        if hv is None or metric not in v2.get(k, {}) or metric not in v3.get(k, {}):
            continue
        trio = {bool(hv), v2[k][metric][0] >= 1.0, v3[k][metric][0] >= 1.0}
        if len(trio) > 1:
            disputed.append(field)
    if not disputed:
        continue
    c = cases[k[0]]
    out = outs[k]
    cits = [cc for cc in (out.get("citations") or []) if isinstance(cc, dict)]
    packages.append(
        {
            "id": f"{k[0]}#{k[1]}",
            "case_id": k[0],
            "attempt": k[1],
            "type": c.type,
            "area": c.area,
            "label_side": x["label_split"],
            "v3_informed": k[0] in V3_INFORMED,
            "question": c.question,
            "reference_answer": c.gold.answer,
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
            "judge_v2": {
                "core_supported": pf(v2[k]["judge_core_supported"][0]),
                "fully_supported": pf(v2[k]["judge_supported"][0]),
                "responsive": pf(v2[k]["judge_responsive"][0]),
                "support_reason": v2[k]["judge_supported"][1],
                "responsive_reason": v2[k]["judge_responsive"][1],
            },
            "judge_v3": {
                "core_supported": pf(v3[k]["judge_core_supported"][0]),
                "fully_supported": pf(v3[k]["judge_supported"][0]),
                "responsive": pf(v3[k]["judge_responsive"][0]),
                "support_reason": v3[k]["judge_supported"][1],
                "responsive_reason": v3[k]["judge_responsive"][1],
            },
        }
    )

results_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
packages_path.write_text(
    json.dumps(
        {
            "rubric_v2": SUPPORT_RUBRICS["v2"],
            "rubric_v3": SUPPORT_RUBRICS["v3"],
            "responsive_prompt": JUDGE_RESPONSIVE_V1,
            "items": packages,
        },
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

# human-readable summary


def interval(est: list[float] | None) -> str:
    return "-" if est is None else f"{est[0]:.2f} ({est[1]:.2f} to {est[2]:.2f})"


for field, _ in VERDICTS:
    e = report["verdicts"][field]
    differ = e["judges_disagree_with_each_other"]
    head = f"\n== {NAMES[field]}: n={e['n']}, labeler fails {e['human_fail']}"
    print(f"{head}, judges differ on {differ}")
    for name in ("v2", "v3"):
        j = e[name]
        agree = f"{j['agree']}/{e['n']} ({j['agree_ci'][0]:.2f} to {j['agree_ci'][1]:.2f})"
        kappa = f"{j['kappa']:.3f} ({j['kappa_ci'][0]:.2f} to {j['kappa_ci'][1]:.2f})"
        counts = "/".join(map(str, j["tp_fp_fn_tn"]))
        print(f"   {name}: agree {agree}; kappa {kappa}")
        print(f"       TPR {interval(j['tpr'])}; TNR {interval(j['tnr'])}; tp/fp/fn/tn {counts}")
    lo, hi = e["delta_kappa_ci"]
    print(
        f"   paired: only v2 agrees {e['only_v2_agrees']}, only v3 agrees {e['only_v3_agrees']}, "
        f"McNemar p = {e['mcnemar_p']:.3g}; kappa v3-v2 {e['delta_kappa_v3_minus_v2']:+.3f} "
        f"({lo:+.2f} to {hi:+.2f})"
    )
    if "labels_needed_to_detect_observed_gain" in e:
        needed = e["labels_needed_to_detect_observed_gain"]
        print(f"   labels needed to detect the observed v3 gain at 80% power: {needed}")
    for sname, s in e["subsets"].items():
        print(
            f"   [{sname}] n={s['n']} fails={s['human_fail']}: "
            f"v2 agree {s['v2_agree']} (kappa {s['v2_kappa']:.2f}), "
            f"v3 agree {s['v3_agree']} (kappa {s['v3_kappa']:.2f})"
        )
print(f"\nDECISION (rule committed in 38c786f): {report['decision']}")
ids = [p["id"] + ":" + ",".join(p["disputed"]) for p in packages]
print(f"disputed answers packaged for adjudication: {len(packages)} -> {ids}")
