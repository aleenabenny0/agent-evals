"""Typed configuration, loaded from YAML. No magic constants elsewhere in the code."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pricing(_Strict):
    """USD per million tokens. Leave as null to report token counts without a dollar figure."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


class ModelConfig(_Strict):
    name: str = "claude-haiku-5-5"
    max_tokens: int = 2048
    temperature: float | None = None
    pricing: Pricing = Pricing()


class DocsAgentConfig(_Strict):
    model: ModelConfig = ModelConfig()
    max_steps: int = 12
    search_k: int = 5
    page_chars: int = 8000
    # Ask the API to enforce tool input schemas. Inputs are also validated locally either way.
    strict_tools: bool = True


class Paths(_Strict):
    corpus_dir: Path = Path("data/snapshots/aws-docs")
    cache_dir: Path = Path(".cache/llm")
    db: Path = Path("results/evals.duckdb")


class RunConfig(_Strict):
    workers: int = 4
    budget_usd: float | None = 5.0


class Config(_Strict):
    paths: Paths = Paths()
    run: RunConfig = RunConfig()
    docs_agent: DocsAgentConfig = DocsAgentConfig()


def load_config(path: Path | None = None) -> Config:
    """Load a YAML config file; missing keys fall back to the defaults above."""
    if path is None:
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
