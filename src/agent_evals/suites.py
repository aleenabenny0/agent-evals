"""Load and validate eval suites (one JSON case per line)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

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
