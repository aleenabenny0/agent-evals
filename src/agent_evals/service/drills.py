"""Prompts for the release drills, and nothing else (docs/plans/phase-8-llm-ops.md, part 6).

They are kept out of SYSTEM_PROMPTS, so no eval can run them, and the registry knows them only
under a config that sets `service.drill`, so a production service refuses them. Each is the
champion's text with one change, the kind a person might make and think an improvement.
"""

from __future__ import annotations

from agent_evals.agents.docs.prompts import SYSTEM_PROMPT_V3

ABSTAIN_RULE = (
    "- If the documentation you can reach does not answer the question, set abstained to true and "
    "explain what is missing. Abstaining is better than guessing.\n"
)
FAULT_RULE = (
    "- Set abstained to true unless the first page you read states the answer word for word. "
    "Abstaining is always better than answering.\n"
)
HEADING = "How to work:\n"
HARMLESS_HEADING = "Work like this:\n"


def _changed(old: str, new: str) -> str:
    if SYSTEM_PROMPT_V3.count(old) != 1:
        raise ValueError(f"the champion's text no longer holds this once: {old!r}")
    return SYSTEM_PROMPT_V3.replace(old, new)


DRILL_FAULT = "drill-fault"
DRILL_HARMLESS = "drill-harmless"
DRILL_PROMPTS = {
    DRILL_FAULT: _changed(ABSTAIN_RULE, FAULT_RULE),  # far too eager to abstain
    DRILL_HARMLESS: _changed(HEADING, HARMLESS_HEADING),  # a heading reworded
}
