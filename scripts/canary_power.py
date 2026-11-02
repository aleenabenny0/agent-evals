"""How the proposed canary rule behaves, by simulation (docs/plans/phase-8-llm-ops.md, part 6).

    uv run python scripts/canary_power.py

The rule looks a few times as the canary's answers come in, and at each look compares the share of
its answers that are bad with the champion's over the same period, by a one-sided Fisher exact
test. Each look is held to the overall false-alarm rate divided by the number of looks, so looking
repeatedly cannot inflate it. This prints, for a baseline bad rate and a canary's true bad rate, how
often the rule fires, and at which look on average. No record is read and no call is made.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from agent_evals.service.canary import ALPHA, LOOKS, fires

SHARE = 0.2  # of traffic that goes to the canary
BASELINES = (0.05, 0.10, 0.20)
LIFTS = (0.0, 0.10, 0.20, 0.30, 0.50)
ROUNDS = 20_000
SEED = 20261101
OUT = Path("results/service/canary-power.json")


def binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(rng.random() < p for _ in range(n))


def one_canary(rng: random.Random, base: float, canary: float) -> int | None:
    """The look at which the rule fires, or None."""
    bad_canary = bad_champion = 0
    seen = 0
    for n, look in enumerate(LOOKS, 1):
        new = look - seen
        seen = look
        bad_canary += binomial(rng, new, canary)
        bad_champion += binomial(rng, round(new * (1 - SHARE) / SHARE), base)
        champion_n = round(look * (1 - SHARE) / SHARE)
        if fires(bad_canary, look, bad_champion, champion_n)[1]:
            return n
    return None


def main() -> None:
    rng = random.Random(SEED)
    rows = []
    for base in BASELINES:
        for lift in LIFTS:
            fired = [one_canary(rng, base, base + lift) for _ in range(ROUNDS)]
            hits = [look for look in fired if look is not None]
            rows.append(
                {
                    "baseline": base,
                    "canary": round(base + lift, 2),
                    "fires": round(len(hits) / ROUNDS, 4),
                    "mean_look": round(sum(hits) / len(hits), 2) if hits else None,
                }
            )
            print(rows[-1], flush=True)
    record = {
        "share": SHARE,
        "looks": list(LOOKS),
        "alpha": ALPHA,
        "rounds": ROUNDS,
        "seed": SEED,
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
