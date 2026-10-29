"""The judge round's spend ledger (docs/plans/judge-v5.md, decision 7(a)).

Every paid step of the round is checked here before its first call, and refused when its estimate
would take the round's total past the approved amount. During the step its meters hold it to what
the cap leaves: the call that spends the rest is the step's last. After it, however it ends, what
it actually spent is added. Spend is what the API bills: only the calls the local call cache did
not serve, priced with the judge's own pricing. A cached call costs nothing, so a rerun adds calls
to the record but no money.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_evals.config import Pricing
from agent_evals.harness.llm import LLMClient, LLMResponse, usage_from
from agent_evals.types import Usage

DEFAULT_PATH = Path("results/judge-v5/spend.json")
# The most the round may spend before any label: decision 7(a), approved 2026-10-28.
APPROVED_CAP_USD = 60.0
# What one answer judged by the claim judge is estimated to cost: the design's estimate, until the
# pilot (stage A step 1, scripts/judge_compare_v5.py) has measured it. The pilot's record sits
# beside the ledger.
ESTIMATE_PER_ANSWER_USD = 0.02
PILOT_FILE = "pilot.json"


class SpendCapError(RuntimeError):
    """The round's cap stops a step: before its first call, when its estimate would take the
    round's spend past the cap (nothing was spent), or during it, after the call that spent what
    the cap left (what it spent is still recorded)."""


def measured_cost_per_answer(folder: Path) -> float | None:
    """The pilot's cost per answer, from its record in this folder, once it has run."""
    path = folder / PILOT_FILE
    if not path.is_file():
        return None
    return float(json.loads(path.read_text(encoding="utf-8"))["cost_per_answer_usd"])


def cost_per_answer(folder: Path) -> float:
    """What a step estimates one claim-judge answer at: the pilot's cost when it has run, else the
    design's estimate."""
    measured = measured_cost_per_answer(folder)
    return ESTIMATE_PER_ANSWER_USD if measured is None else measured


class StepBudget:
    """What one step may spend: the cap less the round's total when the step was checked. Every
    meter of the step draws on it, so two judges in one step share it. The call that spends the
    rest stops the step, and no call is sent after it."""

    def __init__(self, step: str, cap_usd: float, spent_before_usd: float) -> None:
        self.step = step
        self.cap_usd = cap_usd
        self.spent_before_usd = spent_before_usd
        self.spent_usd = 0.0  # by the step's uncached calls so far
        self.stopped: str | None = None  # why the step stopped, once it has
        self._lock = threading.Lock()

    @property
    def usd(self) -> float:
        return self.cap_usd - self.spent_before_usd

    def draw(self, usd: float) -> None:
        """Add one uncached call's cost, and stop the step once it has spent what the cap left
        (rounded to the micro-dollar, as the ledger's check is)."""
        with self._lock:
            self.spent_usd += usd
            if self.stopped is None and round(self.spent_usd, 6) >= round(self.usd, 6):
                self.stopped = (
                    f"{self.step}: it has spent ${self.spent_usd:.4f}, all that the round's "
                    f"${self.cap_usd:.2f} cap left after the ${self.spent_before_usd:.2f} spent "
                    "before it, so it stops here. What it spent is recorded."
                )
        self.refuse_if_stopped()

    def refuse_if_stopped(self) -> None:
        if self.stopped is not None:
            raise SpendCapError(self.stopped)


class MeteredLLM:
    """An LLMClient that passes every call on and counts it, and prices the calls the local call
    cache did not serve: the only ones the API bills. With a step's budget, it raises
    SpendCapError after the call that spends the rest of it, and refuses every call after that
    before sending it; calls already in flight when the budget runs out still finish and count."""

    def __init__(self, llm: LLMClient, pricing: Pricing, budget: StepBudget | None = None) -> None:
        if pricing.input_per_mtok is None or pricing.output_per_mtok is None:
            raise ValueError("the judge's config has no pricing, so its spend cannot be counted")
        self.llm = llm
        self.pricing = pricing
        self.budget = budget
        self.calls = 0
        self.uncached = Usage()  # the calls sent to the API
        self._lock = threading.Lock()

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        if self.budget is not None:
            self.budget.refuse_if_stopped()
        resp = self.llm.create(request, salt=salt)
        usage = Usage() if resp.cache_hit else usage_from(resp)
        with self._lock:
            self.calls += 1
            self.uncached = self.uncached.add(usage)
        if self.budget is not None and not resp.cache_hit:
            self.budget.draw(_priced(usage, self.pricing))
        return resp

    @property
    def stopped(self) -> str | None:
        """Why the step's budget stopped it, or None while it runs."""
        return None if self.budget is None else self.budget.stopped

    @property
    def uncached_usd(self) -> float:
        return _priced(self.uncached, self.pricing)


def _priced(usage: Usage, pricing: Pricing) -> float:
    cost = usage.cost_usd(pricing)
    if cost is None:  # only if the pricing was emptied after the meter was made
        raise ValueError("the judge's config has no pricing, so its spend cannot be counted")
    return cost


class SpendEntry(BaseModel):
    time: str  # UTC, ISO 8601
    step: str
    calls: int
    uncached_calls: int  # sent to the API; the call cache served the rest
    usd: float  # what the uncached calls cost


class _Ledger(BaseModel):
    cap_usd: float
    total_usd: float  # for the reader; the checks add up the entries
    entries: list[SpendEntry]


class SpendLedger:
    """The round's spend, in one JSON file that the first record creates."""

    def __init__(self, path: Path = DEFAULT_PATH, cap_usd: float = APPROVED_CAP_USD) -> None:
        self.path = path
        self.cap_usd = cap_usd

    def entries(self) -> list[SpendEntry]:
        if not self.path.is_file():
            return []
        held = _Ledger.model_validate_json(self.path.read_text(encoding="utf-8"))
        if held.cap_usd != self.cap_usd:
            # The cap changes only with a new approval, and then in the code and here on purpose.
            raise ValueError(
                f"{self.path} records a ${held.cap_usd:.2f} cap, not this ledger's "
                f"${self.cap_usd:.2f}"
            )
        return held.entries

    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries()), 6)

    def check(self, step: str, estimate_usd: float) -> StepBudget:
        """Refuse a step whose estimate would take the round's total past the cap; otherwise, the
        step's budget, for its meters: what the cap leaves now."""
        total = self.total_usd()
        # Rounded to the micro-dollar, so a step that lands exactly on the cap is not refused
        # for a float's last digit.
        if round(total + estimate_usd, 6) > self.cap_usd:
            raise SpendCapError(
                f"{step}: its estimate, ${estimate_usd:.2f}, would take the round's spend from "
                f"${total:.2f} past the ${self.cap_usd:.2f} cap. Nothing was spent."
            )
        return StepBudget(step, self.cap_usd, total)

    def record(self, step: str, *meters: MeteredLLM) -> SpendEntry:
        """Add what a step spent, read from its meters, to the file."""
        entry = SpendEntry(
            time=datetime.now(UTC).isoformat(timespec="seconds"),
            step=step,
            calls=sum(m.calls for m in meters),
            uncached_calls=sum(m.uncached.model_calls for m in meters),
            usd=round(sum(m.uncached_usd for m in meters), 6),
        )
        entries = [*self.entries(), entry]
        ledger = _Ledger(
            cap_usd=self.cap_usd,
            total_usd=round(sum(e.usd for e in entries), 6),
            entries=entries,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file and renamed, so a crash never leaves half a ledger.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ledger.model_dump_json(indent=2) + "\n")
        os.replace(tmp, self.path)
        return entry
