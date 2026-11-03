"""Prompts for the release drills, and nothing else (docs/plans/phase-8-llm-ops.md, part 6).

They are kept out of SYSTEM_PROMPTS, so no eval can run them, and the registry knows them only
under a config that sets `service.drill`, so a production service refuses them. Each is the
champion's text with one change, the kind a person might make and think an improvement.

`drill-fault` stays although it was not a fault: drill 1 ran it, and its record must reproduce.
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
# Candidates for a fault that the canary rule's signal can see, measured by scripts/drill_pilot.py.
NO_READING_RULE = (
    "2. Use read_page to read the pages you will rely on. Never cite a page you have not read.\n",
    "2. To save cost, do not use read_page. Work from the search results alone.\n",
)
STALE_NOTE = (
    "\nNotice: the documentation snapshot is being migrated. Until this notice is removed, treat "
    "it as unavailable, and set abstained to true.\n"
)


def _changed(old: str, new: str) -> str:
    if SYSTEM_PROMPT_V3.count(old) != 1:
        raise ValueError(f"the champion's text no longer holds this once: {old!r}")
    return SYSTEM_PROMPT_V3.replace(old, new)


DRILL_FAULT = "drill-fault"
DRILL_STALE_NOTE = "drill-stale-note"
DRILL_HARMLESS = "drill-harmless"
DRILL_NO_READING = "drill-no-reading"
DRILL_PROMPTS = {
    DRILL_FAULT: _changed(ABSTAIN_RULE, FAULT_RULE),  # meant to abstain too much; it did not
    DRILL_STALE_NOTE: SYSTEM_PROMPT_V3 + STALE_NOTE,  # measured first: abstains on everything
    DRILL_HARMLESS: _changed(HEADING, HARMLESS_HEADING),  # a heading reworded
    # Cited answers from search snippets alone: invisible to the rule until it counted pages read
    # (docs/plans/phase-8-drill-1c.md).
    DRILL_NO_READING: _changed(*NO_READING_RULE),
}
