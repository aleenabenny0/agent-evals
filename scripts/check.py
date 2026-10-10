"""Run everything CI runs, in order, stopping at the first failure. Works on every platform.

    uv run python scripts/check.py          lint, format check, type check, tests, suite validation
    uv run python scripts/check.py --fix    auto-fix lint and formatting first, then the rest

`make check` and the CI workflow both call this script, so there is exactly one definition of
"the checks pass". Tools are run through the current interpreter (`python -m ...`), so the
script needs nothing on PATH beyond the project environment it was started from.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

CHECKS: list[tuple[str, list[str]]] = [
    ("lint", [PY, "-m", "ruff", "check", "."]),
    ("format", [PY, "-m", "ruff", "format", "--check", "."]),
    ("types", [PY, "-m", "mypy"]),
    ("tests", [PY, "-m", "pytest"]),
    ("suites", [PY, "-m", "agent_evals.cli", "validate", "suites/docs/smoke.jsonl"]),
]
FIXES: list[tuple[str, list[str]]] = [
    ("lint --fix", [PY, "-m", "ruff", "check", "--fix", "."]),
    ("format", [PY, "-m", "ruff", "format", "."]),
]


def run(name: str, cmd: list[str]) -> bool:
    print(f"\n== {name}: {' '.join(cmd[2:])}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fix", action="store_true", help="auto-fix lint and formatting first")
    args = parser.parse_args()
    steps = FIXES + CHECKS[2:] if args.fix else CHECKS
    for name, cmd in steps:
        if not run(name, cmd):
            print(f"\nFAILED at {name}.", file=sys.stderr)
            return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
