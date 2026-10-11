"""Load, check, split and write eval suites (one JSON case per line)."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import ValidationError

from agent_evals.corpus import Corpus, normalize, phrase_in
from agent_evals.types import Case


class SuiteError(ValueError):
    pass


def load_suite(path: Path) -> list[Case]:
    cases: list[Case] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = Case.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            errors.append(f"line {lineno}: {exc}")
            continue
        if case.id in seen:
            errors.append(f"line {lineno}: duplicate id {case.id!r}")
        if case.answerable and not case.gold.must_include:
            errors.append(f"line {lineno}: answerable case {case.id!r} has no must_include")
        seen.add(case.id)
        cases.append(case)
    if errors:
        raise SuiteError(f"{path} has {len(errors)} problem(s):\n" + "\n".join(errors))
    return cases


def evidence_index(paths: Iterable[Path]) -> dict[str, list[tuple[str, str]]]:
    """url -> [(suite file, case id)] for every case that lists the url as evidence.

    A lookup aid for the docs drift check, not a validator: files that fail to load are skipped.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for path in paths:
        try:
            cases = load_suite(path)
        except (SuiteError, OSError):
            continue
        for case in cases:
            for url in case.gold.evidence_urls:
                index.setdefault(url, []).append((path.as_posix(), case.id))
    return index


def write_suite(path: Path, cases: Iterable[Case]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for case in cases:
            fh.write(case.model_dump_json(exclude_none=True) + "\n")


def unverified_drafts(cases: Iterable[Case]) -> list[str]:
    """Ids of synthetic cases nobody has verified. They must never be scored by accident."""
    return [c.id for c in cases if c.is_draft]


def unassigned(cases: Iterable[Case]) -> list[str]:
    """Ids of cases that `evals split` has not yet placed in dev or test."""
    return [c.id for c in cases if c.split == "unassigned"]


def check_against_corpus(cases: Iterable[Case], corpus: Corpus) -> tuple[list[str], list[str]]:
    """Checks that need the snapshot. Returns (errors, warnings).

    Errors: an evidence URL that is not in the snapshot (the agent could never cite it), or a
    `support` quote that does not appear on any evidence page (the contract is verbatim).
    Warnings: an answerable case none of whose must_include phrases appears on an evidence page
    (a gold answer may paraphrase, but this catches wrong URLs and invented numbers cheaply),
    and a missing `area`.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for c in cases:
        present = [u for u in c.gold.evidence_urls if u in corpus.pages]
        for url in c.gold.evidence_urls:
            if url not in corpus.pages:
                errors.append(f"{c.id}: evidence page not in snapshot: {url}")
        for quote in c.gold.support:
            if not any(corpus.contains_quote(u, quote) for u in present):
                errors.append(
                    f"{c.id}: support quote not found on any evidence page: {quote[:60]!r}"
                )
        if c.answerable and present:
            quoted = normalize(" ".join(c.gold.support))
            for group in c.gold.must_include:
                if not any(corpus.contains_phrase(u, alt) for u in present for alt in group):
                    warnings.append(
                        f"{c.id}: no must_include alternative found on its evidence pages: "
                        + " | ".join(group)
                    )
                elif c.gold.support and not any(phrase_in(quoted, alt) for alt in group):
                    warnings.append(
                        f"{c.id}: required fact not covered by any support quote: "
                        + " | ".join(group)
                    )
        if c.area is None:
            warnings.append(f"{c.id}: no area")
    return errors, warnings


def stratified_split(
    cases: Sequence[Case], *, dev: int, test: int, seed: int
) -> tuple[list[Case], list[Case]]:
    """Assign dev/test at random within each (type, area) stratum, so both splits share the mix.

    Deterministic for a seed. Uses every case: dev + test must equal len(cases). Test slots are
    allocated to strata in proportion to their size, with the largest remainders rounded up.
    Cases with `hold` set always go to dev.
    """
    if dev < 0 or test < 0 or dev + test != len(cases):
        raise ValueError(f"dev + test must equal the number of cases ({len(cases)})")
    held = [c.model_copy(update={"split": "dev"}) for c in cases if c.hold]
    free = [c for c in cases if not c.hold]
    if test > len(free):
        raise ValueError(f"only {len(free)} cases are not on hold; the test split needs {test}")
    strata: dict[tuple[str, str], list[Case]] = {}
    for c in free:
        strata.setdefault((c.type, c.area or ""), []).append(c)
    frac = test / len(free) if free else 0.0
    quotas = {key: int(len(group) * frac) for key, group in strata.items()}
    remainders = sorted(((len(strata[k]) * frac - quotas[k], k) for k in strata), reverse=True)
    for _, key in remainders[: test - sum(quotas.values())]:
        quotas[key] += 1
    rng = random.Random(seed)
    dev_cases: list[Case] = list(held)
    test_cases: list[Case] = []
    for key in sorted(strata):
        group = sorted(strata[key], key=lambda c: c.id)
        rng.shuffle(group)
        k = quotas[key]
        test_cases += [c.model_copy(update={"split": "test"}) for c in group[:k]]
        dev_cases += [c.model_copy(update={"split": "dev"}) for c in group[k:]]
    return dev_cases, test_cases


# Words that mark the unanswerable classes needing no held-out service: prices, SLAs, dates.
UNANSWERABLE_HINTS = (
    "price",
    "pricing",
    "cost",
    "charge",
    "sla",
    "uptime",
    "when will",
    "what date",
    "which date",
)


def check_held_out(
    cases: Iterable[Case], corpus: Corpus, held_out: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Unanswerable cases rest on an absence; this keeps that absence real. Returns (errors,
    warnings).

    Errors: a held-out service named in a snapshot page title or URL. Warnings: an unanswerable
    case whose question names no held-out service and none of the price / SLA / date hints, so
    its unanswerability should be re-checked by hand.
    """
    names = [h.lower() for h in held_out]
    errors: list[str] = []
    warnings: list[str] = []
    for url, page in corpus.pages.items():
        hay = f"{page.title} {url}".lower()
        for name in names:
            if name in hay:
                errors.append(f"held-out service {name!r} appears in the snapshot: {url}")
    for c in cases:
        if c.answerable:
            continue
        q = c.question.lower()
        if not any(n in q for n in names) and not any(h in q for h in UNANSWERABLE_HINTS):
            warnings.append(
                f"{c.id}: unanswerable case names no held-out service and no price, SLA or "
                "date; confirm the snapshot really lacks the answer"
            )
    return errors, warnings


def paraphrase_groups(cases: Iterable[Case]) -> dict[str, list[str]]:
    """source case id -> ids of its paraphrases (not including the source itself)."""
    groups: dict[str, list[str]] = {}
    for c in cases:
        if c.paraphrase_of:
            groups.setdefault(c.paraphrase_of, []).append(c.id)
    return groups
