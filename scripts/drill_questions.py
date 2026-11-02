"""Write the questions the drills ask: newly simulated, by the phase 7 generator.

    uv run python scripts/drill_questions.py

Costs about $1.30, charged to the phase's ledger. It reads no earlier traffic and nothing from
results/online/, so the answers kept unread for a later judge round stay unread. It reads the test
suite only as the generator always does, to keep clear of the pages test questions rest on. The
call cache means a rerun of the same day spends nothing and writes the same questions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent_evals.config import Pricing, load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.cache import CallCache, sha256_text
from agent_evals.harness.llm import AnthropicClient
from agent_evals.simulate import (
    AREA_MAP_PATH,
    EXTRA_OUTSIDE_SERVICES,
    GENERATOR_VERSION,
    day_summary,
    generate_day,
    load_area_map,
    outside_services,
    page_areas,
    plan_day,
    protected_pages,
)
from agent_evals.spend import (
    PHASE_8_CAP_USD,
    PHASE_8_PATH,
    MeteredLLM,
    SpendCapError,
    SpendLedger,
    StepBudget,
)
from agent_evals.suites import load_suite

CONFIG = Path("configs/default.yaml")
OUT = Path("results/service/drills")
DAY = 801  # far from the phase 7 days, so its seed and its cache salts are its own
HALVES = 2  # 200 questions
MODEL = "claude-sonnet-5-5"
# USD per million tokens, claude-sonnet-5-5, Anthropic first-party rates read 2026-11-02 at
# https://platform.claude.com/docs/en/about-claude/pricing. Reads are 0.05x on this model.
PRICING = Pricing(input_per_mtok=2.0, output_per_mtok=10.0, cache_read_multiplier=0.05)
ESTIMATE_USD = 1.50
MOST_USD = 2.00  # the step stops here, whatever the cap would still allow
STEP = "drill questions"


def main() -> int:
    load_dotenv()
    cfg = load_config(CONFIG)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    test_path = cfg.paths.suites_dir / "docs" / "test.jsonl"
    test_cases = load_suite(test_path)
    protected = protected_pages(corpus, test_cases)
    excluded = set().union(*protected.values())
    pool = sorted(set(corpus.pages) - excluded)
    areas = page_areas(load_area_map(AREA_MAP_PATH), pool)
    candidates = [*cfg.suites.held_out_services, *EXTRA_OUTSIDE_SERVICES]
    services, _ = outside_services(candidates, corpus, [c.question for c in test_cases])
    slots = [
        slot
        for half in range(1, HALVES + 1)
        for slot in plan_day(DAY, corpus, excluded, services, half=half)
    ]
    ledger = SpendLedger(PHASE_8_PATH, PHASE_8_CAP_USD)
    ledger.check(STEP, ESTIMATE_USD)
    budget = StepBudget(STEP, MOST_USD, 0.0)
    meter = MeteredLLM(AnthropicClient(CallCache(cfg.paths.cache_dir)), PRICING, budget)
    try:
        held, redo = generate_day(meter, slots, corpus, areas, MODEL, pool)
    except SpendCapError as exc:
        ledger.record(STEP, meter)
        print(exc, file=sys.stderr)
        return 1
    ledger.record(STEP, meter)
    traffic = "".join(question.model_dump_json() + "\n" for question in held)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "questions.jsonl").write_text(traffic, encoding="utf-8")
    meta = day_summary(held) | {
        "day": DAY,
        "halves": HALVES,
        "generator": MODEL,
        "generator_version": GENERATOR_VERSION,
        "traffic_sha256": sha256_text(traffic),
        "retries": redo,
        "spent_usd": round(meter.uncached_usd, 6),
    }
    (OUT / "questions.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"{len(held)} questions, ${meter.uncached_usd:.4f}, {OUT / 'questions.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
