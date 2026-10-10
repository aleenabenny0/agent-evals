"""Run manifests: everything needed to trace a score back to what produced it."""

from __future__ import annotations

import platform
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from agent_evals.harness.cache import sha256_text
from agent_evals.types import Agent

_TRACKED_PACKAGES = ("anthropic", "duckdb", "pydantic", "rank-bm25", "numpy")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def file_sha256(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


def build_manifest(
    *,
    agent: Agent,
    suite_path: Path,
    corpus_fingerprint: str | None,
    config: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    status = _git("status", "--porcelain")
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "python": platform.python_version(),
        "packages": _package_versions(),
        "agent": {"name": agent.name, "version": agent.version, **agent.describe()},
        "suite": {"path": str(suite_path), "sha256": file_sha256(suite_path)},
        "corpus_fingerprint": corpus_fingerprint,
        "attempts": attempts,
        "config": config,
    }
