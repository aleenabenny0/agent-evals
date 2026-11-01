"""Gate receipts: the record that an agent version was measured before it could serve.

CI holds no API key, so it cannot run the agent, but it does not have to. An agent's version is a
fingerprint of everything that can change its behavior, so CI recomputes it for each prompt the
registry points at, and fails when no receipt in `results/gates/receipts/` names that version. A
change that alters the agent cannot merge unmeasured, and a change that does not alter it never
meets this check. A receipt is written by `evals gate --write-receipt` after a real gate passes.

`basis` says how a receipt came to be. "gate" is a gate that passed on this very version. "carried"
is a version whose fingerprint moved with no change in behavior, and it must say why in `note`: the
test suite recomputes that claim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

RECEIPTS_DIR = Path("results/gates/receipts")


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: str
    prompt_sha256: str
    basis: Literal["gate", "carried"]
    target: str
    champion_run: str
    challenger_run: str
    gated_version: str  # the version the gate really ran on: this one, unless carried
    decided_at: str
    note: str = ""


def receipt_path(version: str, directory: Path = RECEIPTS_DIR) -> Path:
    return directory / f"{version}.json"


def load_receipt(version: str, directory: Path = RECEIPTS_DIR) -> Receipt | None:
    path = receipt_path(version, directory)
    if not path.is_file():
        return None
    return Receipt.model_validate_json(path.read_text(encoding="utf-8"))


def write_receipt(
    *,
    agent_version: str,
    prompt_sha256: str,
    target: str,
    champion_run: str,
    challenger_run: str,
    directory: Path = RECEIPTS_DIR,
) -> Path:
    """The receipt of a gate that passed on this version. A later gate on it leaves the first."""
    path = receipt_path(agent_version, directory)
    if path.exists():
        return path
    receipt = Receipt(
        agent_version=agent_version,
        prompt_sha256=prompt_sha256,
        basis="gate",
        target=target,
        champion_run=champion_run,
        challenger_run=challenger_run,
        gated_version=agent_version,
        decided_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(receipt.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path
