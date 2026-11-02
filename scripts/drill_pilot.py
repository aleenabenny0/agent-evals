"""Measure a candidate fault on a few questions before a drill relies on it (phase 8, part 6).

    uv run python scripts/drill_pilot.py

Drill 1 predicted what its fault prompt would do and was wrong, and the fake provider could not have
shown that, since it is told to abstain under that prompt. So a fault is now measured first. Each
candidate answers the same few questions, through the real agent and the real model with no service
around it, and the table shows each one's share of bad answers by the canary rule's own grading.
Costs a few cents, charged to the phase's ledger.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPT_V3
from agent_evals.config import load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.llm import AnthropicClient
from agent_evals.service.canary import is_bad
from agent_evals.service.drills import NO_READING_RULE, STALE_NOTE, _changed
from agent_evals.spend import PHASE_8_CAP_USD, PHASE_8_PATH, MeteredLLM, SpendLedger, StepBudget
from agent_evals.types import Case, RunContext

CONFIG = Path("configs/service-drill.yaml")
QUESTIONS = Path("results/service/drills/questions.jsonl")
OUT = Path("results/service/drills/pilot.json")
ANSWERABLE = 12  # lookups, from the start of the questions file
STEP = "drill pilot"
ESTIMATE_USD = 0.15
MOST_USD = 0.30
CANDIDATES = {
    "champion": SYSTEM_PROMPT_V3,
    "no-reading": _changed(*NO_READING_RULE),
    "stale-note": SYSTEM_PROMPT_V3 + STALE_NOTE,
}


def lookups() -> list[str]:
    rows = [json.loads(line) for line in QUESTIONS.read_text("utf-8").splitlines() if line.strip()]
    return [row["question"] for row in rows if row["intent"] == "lookup"][:ANSWERABLE]


def answer(agent: DocsAgent, name: str, n: int, question: str) -> dict[str, bool]:
    case = Case(
        id=f"pilot-{name}-{n}",
        suite="online",
        agent="docs",
        type="lookup",
        question=question,
        split="online",
        provenance="simulated",
    )
    result = agent.run(case, RunContext(run_id="pilot", case_id=case.id, attempt=1))
    citations = result.output.get("citations") or []
    abstained = bool(result.output.get("abstained", False))
    errored = bool(result.error)
    bad = is_bad(errored=errored, abstained=abstained, citations=len(citations))
    return {"errored": errored, "abstained": abstained, "bad": bad}


def main() -> None:
    load_dotenv()
    cfg = load_config(CONFIG)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    ledger = SpendLedger(PHASE_8_PATH, PHASE_8_CAP_USD)
    ledger.check(STEP, ESTIMATE_USD)
    meter = MeteredLLM(
        AnthropicClient(), cfg.docs_agent.model.pricing, StepBudget(STEP, MOST_USD, 0.0)
    )
    asked = lookups()
    results = {}
    for name, text in CANDIDATES.items():
        settings = cfg.docs_agent.model_copy(update={"prompt_version": name})
        agent = DocsAgent(meter, corpus, settings, {name: text})
        numbers = range(len(asked))
        names = [name] * len(asked)
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(answer, [agent] * len(asked), names, numbers, asked))
        results[name] = {
            "answers": len(rows),
            "bad": sum(r["bad"] for r in rows),
            "errored": sum(r["errored"] for r in rows),
            "abstained": sum(r["abstained"] for r in rows),
        }
        print(name, results[name], flush=True)
    ledger.record(STEP, meter)
    OUT.write_text(
        json.dumps({"questions": ANSWERABLE, "candidates": results}, indent=1) + "\n", "utf-8"
    )
    print(f"spent ${meter.uncached_usd:.4f}; the phase's total is ${ledger.total_usd():.4f}")


if __name__ == "__main__":
    main()
