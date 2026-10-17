"""Export an error-analysis set: every attempt-1 answer on a DEV run that the validated judge (v2)
fails on "fully supported", with what a reader needs to say why. Dev split only; no test-split
case is read. Writes batch files, one text file per page read, and a context file into OUT.

Usage: uv run python -I -X utf8 scripts/export_unsupported_claims.py OUT [RUN [PROMPT_VERSION]]
Defaults: the v3 champion run of iteration 3 and prompt v3.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.corpus import Corpus
from agent_evals.graders.judge_prompts import SUPPORT_RUBRIC
from agent_evals.store.db import ResultsStore
from agent_evals.suites import load_suite

OUT = Path(sys.argv[1])
RUN = sys.argv[2] if len(sys.argv) > 2 else "docs-20261016-214710-02abb7"
PROMPT = sys.argv[3] if len(sys.argv) > 3 else "v3"
(OUT / "pages").mkdir(parents=True, exist_ok=True)

store = ResultsStore(Path("results/evals.duckdb"))
info = store.run_info(RUN)
attempts = {(a["case_id"], a["attempt"]): a for a in store.all_attempts(RUN)}
v2 = store.grade_reasons(RUN, "judge")
labels = {(x["case_id"], x["attempt"]): x for x in store.labels(RUN, "aleena")}
store.close()
suite_path = Path(info["manifest"]["suite"]["path"])
assert "dev" in suite_path.name and "test" not in suite_path.name, suite_path
cases = {c.id: c for c in load_suite(suite_path)}
corpus = Corpus.load(Path("data/snapshots/aws-docs"))

page_files: dict[str, str] = {}


def page_file(url: str) -> str:
    if url not in page_files:
        n = len(page_files) + 1
        path = OUT / "pages" / f"page-{n:03d}.md"
        page = corpus.pages.get(url)
        text = page.text if page is not None else "(page not in the snapshot)"
        path.write_text(f"URL: {url}\n\n{text}\n", encoding="utf-8")
        page_files[url] = str(path)
    return page_files[url]


items = []
n_answered = 0
for (cid, att), a in sorted(attempts.items()):
    if att != 1 or a["error"] or cid not in cases:
        continue
    out = a["output"]
    if out.get("abstained"):
        continue
    n_answered += 1
    g = v2.get((cid, att), {})
    if "judge_supported" not in g or g["judge_supported"][0] >= 1.0:
        continue
    reads = []
    for step in a["trajectory"]:
        if step.get("kind") == "tool" and step.get("name") == "read_page":
            url = str((step.get("input") or {}).get("url", ""))
            if url and url not in reads:
                reads.append(url)
    cits = [c for c in (out.get("citations") or []) if isinstance(c, dict)]
    for c in cits:
        u = str(c.get("url", ""))
        if u and u not in reads:
            reads.append(u)
    lab = labels.get((cid, att))
    items.append(
        {
            "id": f"{cid}#{att}",
            "type": cases[cid].type,
            "area": cases[cid].area,
            "question": cases[cid].question,
            "answer": str(out.get("answer", "")),
            "citations": [(str(c.get("url", "")), str(c.get("quote", ""))) for c in cits],
            "judge_core": "pass" if g["judge_core_supported"][0] >= 1.0 else "fail",
            "judge_reason": g["judge_supported"][1],
            "label": None
            if lab is None
            else {
                "core": lab.get("core_supported"),
                "fully": lab.get("supported"),
                "note": lab.get("note") or "",
            },
            "pages": [(u, page_file(u)) for u in reads],
        }
    )


def render(item: dict) -> str:
    lines = [
        f"=== ITEM {item['id']} ===",
        f"type: {item['type']}; area: {item['area']}",
        f"QUESTION: {item['question']}",
        "ANSWER:",
        item["answer"],
        "CITED QUOTES:",
    ]
    for i, (u, q) in enumerate(item["citations"], 1):
        lines.append(f'  {i}. ({u}) "{q}"')
    lines.append(
        f"VALIDATED JUDGE: fully supported = fail; core supported = {item['judge_core']}. "
        f"Its reason (names the first unsupported claim it found): {item['judge_reason']}"
    )
    if item["label"] is not None:
        lab = item["label"]
        yn = {True: "yes", False: "no", None: "-"}
        lines.append(
            f"HUMAN LABEL: core supported = {yn[lab['core']]}, fully supported = "
            f"{yn[lab['fully']]}. Labeler's note: {lab['note'] or '(none)'}"
        )
    lines.append("PAGES THE AGENT READ OR CITED (search these files with Grep):")
    for u, f in item["pages"]:
        lines.append(f"  {u} -> {f}")
    lines.append("")
    return "\n".join(lines)


batches = [items[i::3] for i in range(3)]
for i, b in enumerate(batches, 1):
    (OUT / f"batch-{i}.txt").write_text("\n".join(render(x) for x in b), encoding="utf-8")
# An independent second reading of 15 items (5 per batch), to measure how reliable the counts are.
second = [x for b in batches for x in b[2::5][:5]]
(OUT / "second-reading.txt").write_text("\n".join(render(x) for x in second), encoding="utf-8")
(OUT / "context.md").write_text(
    "# Context for the iteration-3 error analysis\n\n"
    "## The judge's support rubric (the standard every verdict here applies)\n\n"
    + SUPPORT_RUBRIC
    + f"\n\n## The docs agent's system prompt on this run ({PROMPT})\n\n"
    + SYSTEM_PROMPTS[PROMPT],
    encoding="utf-8",
)
(OUT / "items.json").write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
by_type: dict[str, int] = {}
for x in items:
    by_type[x["type"]] = by_type.get(x["type"], 0) + 1
print(f"attempt-1 answers on the dev run: {n_answered}; failing fully supported: {len(items)}")
print(f"labeled among them: {sum(1 for x in items if x['label'] is not None)}")
print(f"batches: {[len(b) for b in batches]}; second reading: {[x['id'] for x in second]}")
print(f"pages exported: {len(page_files)}; by type: {by_type}")
print(f"core fails among them: {sum(1 for x in items if x['judge_core'] == 'fail')}")
