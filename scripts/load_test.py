"""A load test of the running service.

    uv run evals serve -c configs/service-fake.yaml --port 8011
    uv run python scripts/load_test.py --url http://127.0.0.1:8011 --label fake --rates 1,2,4,8

Questions arrive at a set rate whether or not earlier ones have finished. That is an open loop, as
real traffic is: a closed loop, where each client waits for its reply, slows down with the service
and hides the overload it was meant to find. Each rate runs for a while, and the table shows where
latency turns up and how the limits behave past that point.

Run it against the fake provider and it costs nothing. Against the real one it costs what the
answers cost, so a real run is short, the service's daily cap still holds, and --paid-config checks
it against the phase's ledger first and adds what it spent afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from agent_evals.config import load_config
from agent_evals.service.budget import DailyBudget
from agent_evals.spend import PHASE_8_CAP_USD, PHASE_8_PATH, SpendLedger

SUITE = Path("suites/docs/smoke.jsonl")
OUT = Path("results/service")
SEED = 20261101


def questions(path: Path = SUITE) -> list[str]:
    rows = path.read_text(encoding="utf-8").splitlines()
    return [str(json.loads(row)["question"]) for row in rows if row.strip()]


def percentile(values: Sequence[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(share * len(ordered)), len(ordered) - 1)]


async def one(
    client: httpx.AsyncClient, question: str, statuses: Counter[str], latencies: list[float]
) -> None:
    started = time.perf_counter()
    try:
        response = await client.post("/v1/ask", json={"question": question})
    except httpx.HTTPError as exc:
        statuses[type(exc).__name__] += 1
        return
    statuses[str(response.status_code)] += 1
    if response.status_code == 200:
        latencies.append(time.perf_counter() - started)


async def step(
    client: httpx.AsyncClient, rate: float, seconds: float, asked: Sequence[str], rng: random.Random
) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    latencies: list[float] = []
    tasks: list[asyncio.Task[None]] = []
    started = time.perf_counter()
    # Arrivals are placed on the clock, not by sleeping one gap at a time: a short sleep can return
    # early, and on Windows it does, which once sent half as much again as the rate asked for.
    at = 0.0
    while True:
        at += rng.expovariate(rate)  # the gaps of a Poisson arrival process
        if at >= seconds:
            break
        wait = started + at - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        question = rng.choice(asked)
        tasks.append(asyncio.create_task(one(client, question, statuses, latencies)))
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started
    answered = statuses["200"]
    return {
        "rate_per_s": rate,
        "sent": len(tasks),
        "offered_per_s": round(len(tasks) / seconds, 3),
        "statuses": dict(sorted(statuses.items())),
        "answered_per_s": round(answered / elapsed, 3),
        "refused_share": round(1 - answered / len(tasks), 4) if tasks else 0.0,
        "latency_s": {
            name: None if (v := percentile(latencies, share)) is None else round(v, 3)
            for name, share in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
    }


async def run(
    url: str, rates: Sequence[float], seconds: float, asked: Sequence[str]
) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    limits = httpx.Limits(max_connections=None)
    results = []
    async with httpx.AsyncClient(base_url=url, timeout=300.0, limits=limits) as client:
        ready = (await client.get("/readyz")).json()
        if not ready["ready"]:
            raise SystemExit(f"the service is not ready: {ready}")
        for rate in rates:
            found = await step(client, rate, seconds, asked, rng)
            latency = found["latency_s"]
            print(
                f"{rate:>6} per s: offered {found['offered_per_s']:>7}, answered "
                f"{found['answered_per_s']:>6} per s, p50 {latency['p50']}, p95 {latency['p95']}, "
                f"p99 {latency['p99']}, {found['statuses']}",
                flush=True,
            )
            results.append(found)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--label", required=True, help="Names the results file.")
    parser.add_argument("--rates", default="1,2,4,8", help="Questions per second, in steps.")
    parser.add_argument("--seconds", type=float, default=30.0, help="How long each rate runs.")
    parser.add_argument("--questions", type=Path, default=SUITE)
    parser.add_argument("--paid-config", type=Path, help="The real service's config, for spend.")
    parser.add_argument("--estimate-usd", type=float, default=1.0, help="Checked against the cap.")
    args = parser.parse_args()
    rates = [float(r) for r in args.rates.split(",")]
    step = f"load test {args.label}"
    ledger = SpendLedger(PHASE_8_PATH, PHASE_8_CAP_USD)
    budget = None
    if args.paid_config:
        service = load_config(args.paid_config).service
        budget = DailyBudget(service.spend_path, service.daily_budget_usd)
        ledger.check(step, args.estimate_usd)
    before = budget.spent_usd() if budget else 0.0
    results = asyncio.run(run(args.url, rates, args.seconds, questions(args.questions)))
    record: dict[str, Any] = {"label": args.label, "seconds_per_rate": args.seconds}
    if budget:
        time.sleep(1.0)  # the last answers' spend reaches the file as their threads end
        spent = budget.spent_usd() - before
        answers = sum(step_["statuses"].get("200", 0) for step_ in results)
        ledger.add(step, calls=answers, usd=spent)
        record["spent_usd"] = round(spent, 6)
        print(f"spent ${spent:.4f}; the phase's total is ${ledger.total_usd():.4f}")
    record["steps"] = results
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"load-{args.label}.json"
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"written: {path}")


if __name__ == "__main__":
    main()
