"""The eval gate in CI: every agent version the registry serves must have a gate receipt.

    uv run python scripts/check_gate_receipt.py

Free and offline. It recomputes the agent's version fingerprint for the prompts the registry points
at, with the settings the gated runs used, and fails when a version has no receipt. So a pull
request that changes the agent's behavior cannot merge until a real gate has passed on it, and CI
needs no API key. docs/plans/phase-8-llm-ops.md, part 5.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.config import Config, load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.llm import LLMResponse
from agent_evals.receipts import load_receipt
from agent_evals.service.registry import PromptRegistry

CONFIG = Path("configs/default.yaml")


class NoModel:
    """The fingerprint needs no model, and nothing here may ever call one."""

    def create(self, request: dict[str, Any], *, salt: str) -> LLMResponse:
        raise RuntimeError("the receipt check calls no model")


def served_versions(config: Config) -> dict[str, str]:
    """The agent version of each prompt the registry points at, by role."""
    registry = PromptRegistry(
        config.service.registry_path, config.service.registry_log, SYSTEM_PROMPTS
    )
    state = registry.state()
    pointers = {"champion": state.champion, "canary": state.canary}
    return {
        role: DocsAgent(
            NoModel(),
            Corpus([]),
            config.docs_agent.model_copy(update={"prompt_version": pointer.version}),
        ).version
        for role, pointer in pointers.items()
        if pointer is not None
    }


def missing(versions: dict[str, str], directory: Path | None = None) -> list[str]:
    found = [
        f"the {role}, agent version {version}, has no gate receipt"
        for role, version in versions.items()
        if (load_receipt(version) if directory is None else load_receipt(version, directory))
        is None
    ]
    return found


def main() -> int:
    versions = served_versions(load_config(CONFIG))
    problems = missing(versions)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            "This change alters the agent, so it must be measured before it merges: run it on the "
            "dev suite, then uv run evals gate CHAMPION CHALLENGER --write-receipt (ADR 0008).",
            file=sys.stderr,
        )
        return 1
    print("every served agent version has a gate receipt: " + ", ".join(versions.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
