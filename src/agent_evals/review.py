"""Helpers for reviewing drafted cases: the human step of ADR 0006, made fast.

`evals review` shows each unverified draft with its support quote highlighted inside the page
text and records accept / reject decisions. The pure parts live here so they can be tested; the
terminal loop is in `cli.py`.
"""

from __future__ import annotations

import re

from agent_evals.types import Case


def quote_context(text: str, quote: str, *, width: int = 300) -> tuple[str, str, str] | None:
    """Locate `quote` in raw page text, tolerating whitespace and case differences.

    Returns (before, match, after) with `width` characters of context on each side, or None
    when the quote cannot be found in the raw text (it may still match after normalization).
    """
    words = [re.escape(w) for w in quote.split()]
    if not words:
        return None
    match = re.compile(r"\s+".join(words), re.IGNORECASE).search(text)
    if match is None:
        return None
    start, end = match.span()
    return text[max(0, start - width) : start], match.group(0), text[end : end + width]


def accept(case: Case, reviewer: str) -> Case:
    """The reviewed copy: same case, now verified. Nothing else changes."""
    return case.model_copy(update={"verified_by": reviewer})


def reject(case: Case, reviewer: str, reason: str) -> Case:
    """A copy marked for the rejected file, with the reason kept in `notes`. Still unverified."""
    note = f"REJECTED by {reviewer}: {reason.strip() or 'no reason given'}"
    notes = f"{case.notes} | {note}" if case.notes else note
    return case.model_copy(update={"notes": notes})


REJECTION_MARK = " | REJECTED by "


def restore(case: Case) -> Case:
    """Undo a rejection: drop the rejection note (keeping any earlier notes), still unverified."""
    notes = case.notes or ""
    if notes.startswith("REJECTED by "):
        kept: str | None = None
    elif REJECTION_MARK in notes:
        kept = notes.split(REJECTION_MARK, 1)[0]
    else:
        kept = notes or None
    return case.model_copy(update={"notes": kept, "verified_by": None})
