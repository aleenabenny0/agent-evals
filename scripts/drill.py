"""A release drill against the running service (docs/plans/phase-8-llm-ops.md, part 6).

    uv run python scripts/drill.py --label fault --canary drill-fault -c configs/service-drill.yaml

Questions arrive at a steady rate whether or not earlier ones have finished, as in the load test.
After a warm-up on the champion a canary goes live at a share of traffic, and the drill then waits
for the service to act on its own: it never rolls anything back. It stops soon after a rollback, or
at its limit of questions or money, and writes every request it saw, without the question's text.

With no --canary it is a plain stretch of traffic, for the failover drill: --block-after and
--unblock-after run a command part way through, which is how the primary is taken away and brought
back.

A paid drill is checked against the phase's ledger before its first question, and what it spent is
added afterwards, read from the service's own spend file so that timed-out answers count too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent_evals.config import Config, load_config
from agent_evals.service.budget import DailyBudget
from agent_evals.service.canary import is_bad
from agent_evals.service.drills import DRILL_PROMPTS
from agent_evals.service.main import prompts_of, registry_of
from agent_evals.service.registry import PromptRegistry
from agent_evals.spend import PHASE_8_CAP_USD, PHASE_8_PATH, SpendLedger

OUT = Path("results/service/drills")
QUESTIONS = OUT / "questions.jsonl"
SEED = 20261102


def questions(path: Path = QUESTIONS) -> list[str]:
    rows = path.read_text(encoding="utf-8").splitlines()
    return [str(json.loads(row)["question"]) for row in rows if row.strip()]


async def one(
    client: httpx.AsyncClient, question: str, sent_s: float, rows: list[dict[str, Any]]
) -> None:
    started = time.perf_counter()
    row: dict[str, Any] = {"sent_s": round(sent_s, 2)}
    try:
        response = await client.post("/v1/ask", json={"question": question})
    except httpx.HTTPError as exc:
        rows.append(row | {"status": type(exc).__name__})
        return
    row |= {
        "status": response.status_code,
        "latency_s": round(time.perf_counter() - started, 2),
    }
    body = response.json()
    if response.status_code == 200:
        row |= {
            "role": body["role"],
            "version": body["prompt_version"],
            "abstained": body["abstained"],
            "citations": len(body["citations"]),
            "pages_read": body["pages_read"],
            "providers": body["providers"],
            "model_calls": body["model_calls"],
            "cost_usd": body["cost_usd"],
        }
    else:
        row["outcome"] = body.get("error")
    rows.append(row)


def summary(
    rows: Sequence[Mapping[str, Any]], canary_at: float | None, rolled_at: float | None
) -> dict[str, Any]:
    """What the drill saw from outside the service. An error carries no role, so the two sides'
    counts are of answers that came back."""
    roles: dict[str, Counter[str]] = {}
    for row in rows:
        if row["status"] == 200:
            held = roles.setdefault(str(row["role"]), Counter())
            held["answers"] += 1
            held["bad"] += is_bad(
                errored=False,
                abstained=bool(row["abstained"]),
                citations=int(row["citations"]),
                pages=int(row["pages_read"]),
            )
            held["read_no_page"] += int(row["pages_read"]) == 0
    sent_after = [] if rolled_at is None else [r for r in rows if r["sent_s"] > rolled_at]
    canary_sent = [r["sent_s"] for r in rows if r.get("role") == "canary"]
    return {
        "sent": len(rows),
        "statuses": dict(sorted(Counter(str(r["status"]) for r in rows).items())),
        "roles": {role: dict(held) for role, held in sorted(roles.items())},
        "canary_live_s": canary_at,
        "first_canary_sent_s": min(canary_sent) if canary_sent else None,
        "rollback_seen_s": rolled_at,
        "seconds_to_rollback": (
            None if rolled_at is None or canary_at is None else round(rolled_at - canary_at, 1)
        ),
        "canary_answers_sent_after_rollback": sum(r.get("role") == "canary" for r in sent_after),
        "cost_seen_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 6),
    }


async def drive(
    args: argparse.Namespace, registry: PromptRegistry, asked: Sequence[str]
) -> dict[str, Any]:
    rng = random.Random(f"{SEED}-{args.label}")
    rows: list[dict[str, Any]] = []
    tasks: list[asyncio.Task[None]] = []
    canary_at: float | None = None
    rolled_at: float | None = None
    blocked_at: float | None = None
    unblocked_at: float | None = None
    run: str | None = None
    limits = httpx.Limits(max_connections=None)
    async with httpx.AsyncClient(base_url=args.url, timeout=300.0, limits=limits) as client:
        ready = (await client.get("/readyz")).json()
        if not ready["ready"]:
            raise SystemExit(f"the service is not ready: {ready}")
        began = time.perf_counter()
        at = 0.0
        sent = 0
        stop_at = args.most
        while sent < stop_at and spent_so_far(rows) < args.budget_usd:
            at += rng.expovariate(args.rate)
            wait = began + at - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            now = round(time.perf_counter() - began, 2)
            if args.canary and canary_at is None and sent >= args.warmup:
                state = registry.set_canary(
                    args.canary, args.share, who="drill", why=f"release drill {args.label}"
                )
                run, canary_at = state.canary_run, now
                print(f"{now:>8}s canary {args.canary} live at {args.share:.0%}", flush=True)
            if canary_at is not None and rolled_at is None and registry.state().canary_run != run:
                rolled_at = now
                stop_at = min(stop_at, sent + args.after)
                print(f"{now:>8}s the canary is gone, with {sent} questions sent", flush=True)
            if args.block_after is not None and blocked_at is None and sent >= args.block_after:
                subprocess.run(args.block_command, shell=True, check=True)
                blocked_at = now
                print(f"{now:>8}s primary blocked, with {sent} questions sent", flush=True)
            if (
                args.unblock_after is not None
                and unblocked_at is None
                and sent >= args.unblock_after
            ):
                subprocess.run(args.unblock_command, shell=True, check=True)
                unblocked_at = now
                print(f"{now:>8}s primary back, with {sent} questions sent", flush=True)
            question = rng.choice(asked)
            tasks.append(asyncio.create_task(one(client, question, now, rows)))
            sent += 1
        await asyncio.gather(*tasks)
    rows.sort(key=lambda row: float(row["sent_s"]))
    return {
        "label": args.label,
        "canary": args.canary,
        "share": args.share if args.canary else None,
        "rate_per_s": args.rate,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "blocked_s": blocked_at,
        "unblocked_s": unblocked_at,
        "summary": summary(rows, canary_at, rolled_at),
        "rows": rows,
    }


def spent_so_far(rows: Sequence[Mapping[str, Any]]) -> float:
    return sum(float(row.get("cost_usd") or 0.0) for row in rows)


def paid(config: Config) -> bool:
    return config.service.fake_latency_s is None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--label", required=True, help="Names the results file.")
    parser.add_argument("-c", "--config", type=Path, default=Path("configs/service-drill.yaml"))
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--canary", choices=sorted(DRILL_PROMPTS), help="The version to release.")
    parser.add_argument("--share", type=float, default=0.2)
    parser.add_argument("--rate", type=float, default=0.5, help="Questions per second.")
    parser.add_argument("--warmup", type=int, default=25, help="Questions before the canary.")
    parser.add_argument("--most", type=int, default=340, help="The most questions to send.")
    parser.add_argument("--after", type=int, default=20, help="Questions after a rollback.")
    parser.add_argument("--budget-usd", type=float, default=2.0, help="Stop at this spend.")
    parser.add_argument("--estimate-usd", type=float, default=1.5, help="Checked against the cap.")
    parser.add_argument("--block-after", type=int, help="Run --block-command after this many.")
    parser.add_argument("--block-command", default="", help="How the primary is taken away.")
    parser.add_argument("--unblock-after", type=int, help="Run --unblock-command after this many.")
    parser.add_argument("--unblock-command", default="", help="How the primary comes back.")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    args = parser.parse_args()

    config = load_config(args.config)
    if args.canary:
        if not config.service.drill:
            raise SystemExit(f"{args.config} does not set service.drill")
        if args.canary not in prompts_of(config):
            raise SystemExit(f"{args.canary} is not a prompt this config knows")
    step = f"drill {args.label}"
    ledger = SpendLedger(PHASE_8_PATH, PHASE_8_CAP_USD)
    budget = DailyBudget(config.service.spend_path, config.service.daily_budget_usd)
    if paid(config):
        ledger.check(step, args.estimate_usd)
    before = budget.spent_usd()
    registry = registry_of(config)
    if registry.state().canary is not None:
        raise SystemExit("a canary is already live: this drill starts from none")
    record = asyncio.run(drive(args, registry, questions(args.questions)))
    if paid(config):
        time.sleep(1.0)  # the last answers' spend reaches the file as their threads end
        spent = max(budget.spent_usd() - before, record["summary"]["cost_seen_usd"])
        answers = sum(row["status"] == 200 for row in record["rows"])
        ledger.add(step, calls=answers, usd=spent)
        record["spent_usd"] = round(spent, 6)
        print(f"spent ${spent:.4f}; the phase's total is ${ledger.total_usd():.4f}")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.label}.json"
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(record["summary"], indent=1))
    print(f"written: {path}")


if __name__ == "__main__":
    main()
