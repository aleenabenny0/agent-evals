"""Load, check, split and write eval suites (one JSON case per line)."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import ValidationError

from agent_evals.corpus import Corpus
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
            for url in case.gold.evidence:
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
        present = [u for u in c.gold.evidence if u in corpus.pages]
        for url in c.gold.evidence:
            if url not in corpus.pages:
                errors.append(f"{c.id}: evidence page not in snapshot: {url}")
        if c.gold.support is not None and not any(
            corpus.contains_quote(u, c.gold.support) for u in present
        ):
            errors.append(
                f"{c.id}: support quote not found on any evidence page: {c.gold.support[:60]!r}"
            )
        if c.answerable and present:
            for group in c.gold.must_include:
                if not any(corpus.contains_quote(u, alt) for u in present for alt in group):
                    warnings.append(
                        f"{c.id}: no must_include alternative found on its evidence pages: "
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
    """
    if dev < 0 or test < 0 or dev + test != len(cases):
        raise ValueError(f"dev + test must equal the number of cases ({len(cases)})")
    strata: dict[tuple[str, str], list[Case]] = {}
    for c in cases:
        strata.setdefault((c.type, c.area or ""), []).append(c)
    frac = test / len(cases) if cases else 0.0
    quotas = {key: int(len(group) * frac) for key, group in strata.items()}
    remainders = sorted(((len(strata[k]) * frac - quotas[k], k) for k in strata), reverse=True)
    for _, key in remainders[: test - sum(quotas.values())]:
        quotas[key] += 1
    rng = random.Random(seed)
    dev_cases: list[Case] = []
    test_cases: list[Case] = []
    for key in sorted(strata):
        group = sorted(strata[key], key=lambda c: c.id)
        rng.shuffle(group)
        k = quotas[key]
        test_cases += [c.model_copy(update={"split": "test"}) for c in group[:k]]
        dev_cases += [c.model_copy(update={"split": "dev"}) for c in group[k:]]
    return dev_cases, test_cases
