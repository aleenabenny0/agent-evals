"""The canary rule: is a new prompt version worse than the champion, while it serves?

A bad answer is one that errored, abstained or came back with no citation, all of which code can
grade at once. After 20, 40 and 60 canary answers the canary's share of bad answers is compared
with the champion's over the same period, by a one-sided Fisher exact test. Each look is held to
0.33%, so the chance of a false alarm stays under 1% however many looks are taken
(docs/plans/phase-8-llm-ops.md, part 6, and scripts/canary_power.py, which simulates this rule).

A rule that fires stops the canary at once. A canary that reaches the last look is left in place
for a person, since passing shows only the absence of a large fault, and the rule cannot see a
prompt that gives confident, cited, wrong answers. That is what the gate before it is for.

The counts live in memory, so a restart starts the looks again. A restart delays a rollback, and
each one spends the false-alarm allowance again, so the 1% holds for a canary with no restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_evals.service.registry import Role
from agent_evals.stats.monitor import fisher_greater

LOOKS = (20, 40, 60)  # canary answers at each look
ALPHA = 0.01  # overall false-alarm rate per canary
PER_LOOK = ALPHA / len(LOOKS)


def is_bad(*, errored: bool, abstained: bool = False, citations: int = 0) -> bool:
    """The rule's one signal for one answer."""
    return errored or abstained or citations == 0


def fires(bad_canary: int, n_canary: int, bad_champion: int, n_champion: int) -> tuple[float, bool]:
    """One look: its p-value, and whether the canary is worse beyond what chance allows."""
    p = fisher_greater(bad_canary, n_canary, bad_champion, n_champion)
    return p, p < PER_LOOK


@dataclass(frozen=True)
class Look:
    number: int  # 1 for the first look
    canary_bad: int
    canary_answers: int
    champion_bad: int
    champion_answers: int
    p: float
    fired: bool

    @property
    def last(self) -> bool:
        return self.number == len(LOOKS)

    def why(self) -> str:
        return (
            f"canary rule, look {self.number}: {self.canary_bad} of {self.canary_answers} canary "
            f"answers bad, {self.champion_bad} of {self.champion_answers} on the champion, "
            f"p = {self.p:.5f}"
        )


class CanaryWatch:
    """The counts of one canary run. Only the service's event loop calls this, so it has no lock."""

    def __init__(self) -> None:
        self._run: str | None = None
        self._clear()

    def _clear(self) -> None:
        self._answers: dict[Role, int] = {"champion": 0, "canary": 0}
        self._bad: dict[Role, int] = {"champion": 0, "canary": 0}
        self._looks = 0
        self._fired = False

    def follow(self, run: str | None) -> None:
        """Start again when the registry names a different run, or none."""
        if run != self._run:
            self._run = run
            self._clear()

    @property
    def stopped(self) -> bool:
        """The rule has fired on the run being followed, so that run gets no more traffic, even if
        the registry file could not be changed."""
        return self._fired

    def record(self, run: str | None, role: Role, bad: bool) -> Look | None:
        """Count one finished answer, and return the look it completes, if any. An answer from
        another run does not count, and once the rule has decided, nothing does."""
        if run is None or run != self._run or self._fired or self._looks == len(LOOKS):
            return None
        self._answers[role] += 1
        self._bad[role] += bad
        if role != "canary" or self._answers["canary"] != LOOKS[self._looks]:
            return None
        self._looks += 1
        counts = (
            self._bad["canary"],
            self._answers["canary"],
            self._bad["champion"],
            self._answers["champion"],
        )
        p, fired = fires(*counts)
        self._fired = fired
        return Look(self._looks, *counts, p, fired)
