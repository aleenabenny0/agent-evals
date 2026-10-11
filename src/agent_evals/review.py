"""Helpers for reviewing drafted cases: the human step of ADR 0006, made fast.

`evals review` shows each unverified draft with its support quote highlighted inside the page
text and records accept / reject decisions. The pure parts live here so they can be tested; the
terminal loop is in `cli.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agent_evals.corpus import normalize, phrase_in
from agent_evals.entities import NUMBER, numbers_in
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


_IDENTIFIER = re.compile(r"[A-Za-z]*\d[\w.\-/]*|/\d+")  # python3.10, AL2023, IMDSv2, /16


def answer_terms(case: Case) -> list[str]:
    """What a reviewer must find on the page: every required phrase, every standalone number and
    every identifier with a digit in it (runtime names, versions, CIDR sizes) from the gold
    answer. Anything the question already states is left out; it is not a claim. Required
    phrases first, then numbers, then identifiers, no duplicates."""
    terms = [alt for group in case.gold.must_include for alt in group]
    asked = numbers_in(case.question)
    terms += sorted(numbers_in(case.gold.answer) - asked, key=float)
    asked_ids = {t.lower() for t in _IDENTIFIER.findall(case.question)}
    terms += [
        t.rstrip(".,;:")
        for t in _IDENTIFIER.findall(case.gold.answer)
        if t.lower() not in asked_ids and not NUMBER.fullmatch(t.rstrip(".,;:"))
    ]
    return list(dict.fromkeys(t for t in terms if t.strip()))


def _term_pattern(term: str) -> str:
    if re.fullmatch(r"\d+(?:\.\d+)?", term):
        # A number as the page may print it: "10240" also matches "10,240"; never a digit run
        # inside a longer number or an identifier.
        whole, _, frac = term.partition(".")
        groups: list[str] = []
        while len(whole) > 3:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        groups.insert(0, whole)
        body = ",?".join(groups) + (rf"\.{frac}" if frac else "")
        return rf"(?<![\w.]){body}(?!\w)(?!\.\d)"
    words = r"\s+".join(re.escape(w) for w in term.split())
    head = r"(?<!\w)" if term[0].isalnum() else ""
    tail = r"(?!\w)" if term[-1].isalnum() else ""
    return f"{head}{words}{tail}"


def term_spans(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    """Character spans in raw page text where any term occurs: whole words, any case, any
    whitespace. Longer terms win where two overlap, and spans never overlap."""
    terms = [t for t in terms if t.strip()]
    if not terms:
        return []
    ordered = sorted(terms, key=len, reverse=True)
    pattern = re.compile("|".join(_term_pattern(t) for t in ordered), re.IGNORECASE)
    return [m.span() for m in pattern.finditer(text)]


def uncovered_terms(case: Case) -> list[str]:
    """Answer facts no support quote states: required-phrase groups with no alternative in the
    quotes, and numbers in the gold answer that are in neither the quotes nor the question.
    Mirrors the warnings `evals validate` prints, so the reviewer sees the same gaps."""
    if not case.gold.support:
        return []
    quoted = normalize(" ".join(case.gold.support))
    gaps = [
        " | ".join(group)
        for group in case.gold.must_include
        if not any(phrase_in(quoted, alt) for alt in group)
    ]
    quoted_numbers = numbers_in(" ".join(case.gold.support))
    loose = numbers_in(case.gold.answer) - numbers_in(case.question) - quoted_numbers
    return gaps + sorted(loose, key=float)


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
