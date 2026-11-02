"""Typed configuration, loaded from YAML. No magic constants elsewhere in the code."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pricing(_Strict):
    """USD per million tokens. Leave as null to report token counts without a dollar figure."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    # The API's prompt cache, as multiples of the input price, for the 5-minute cache. Reads are
    # 0.1x on claude-haiku-5-5 and 0.05x on claude-sonnet-5-5 (pricing in the prompt caching
    # docs, read 2026-10-19).
    cache_write_multiplier: float = 1.25
    cache_read_multiplier: float = 0.1


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
    # Rejected submit_answer calls (wrong shape or broken content rules) allowed before the
    # attempt fails with error `malformed_submission` instead of spending its remaining steps.
    max_submission_rejections: int = 3
    # Which system prompt the docs agent uses (keys of agents.docs.prompts.SYSTEM_PROMPTS). The
    # champion stays here; a challenger config file overrides it for a gated comparison.
    prompt_version: str = "v2"
    # Iteration 2 challenger: reject a submission whose answer states a number found in neither
    # the question nor any cited quote, so the model quotes it or drops it before submitting.
    require_number_coverage: bool = False


class Paths(_Strict):
    corpus_dir: Path = Path("data/snapshots/aws-docs")
    # Committed hash-per-page record of the snapshot, for the monthly drift check (ADR 0005).
    docs_lock: Path = Path("configs/docs_pages.lock.json")
    suites_dir: Path = Path("suites")
    cache_dir: Path = Path(".cache/llm")
    db: Path = Path("results/evals.duckdb")


class RunConfig(_Strict):
    workers: int = 4
    budget_usd: float | None = 5.0


class GradersConfig(_Strict):
    # A quote shorter than this can't be verified against a page and counts as fabricated.
    min_quote_chars: int = 12


class JudgeConfig(_Strict):
    """The LLM judge (docs/plans/llm-judge.md). Runs post hoc via `evals judge`, never in `run`."""

    # Sonnet 5.5 thinks for a few hundred tokens before the tool call; 400 truncated the verdict.
    model: ModelConfig = ModelConfig(name="claude-sonnet-5-5", max_tokens=2048)
    strict_tools: bool = True
    # Grader name the judge's grades are stored under. A candidate judge runs under another name
    # (for example "judge-v3") so two judges can score one run and be compared against labels.
    grader_name: str = "judge"
    # Which support rubric the judge and the label screen read (keys of SUPPORT_RUBRICS).
    rubric_version: str = "v2"
    # "verdict": one call returns the two support verdicts (judges v2 to v4). "claims": the claim
    # judge (docs/plans/judge-v5.md), a claim table that code checks, then a skeptic pass.
    method: Literal["verdict", "claims"] = "verdict"
    # The claim judge's two thresholds, both part of its version: a claim is supported at this
    # score or above (t), and the skeptic's search for a missed claim fails the answer at this
    # confidence or above (s; null turns that search off). The claim judge refuses any value off
    # stage A's grid (judge v6: t 64, 69 or 74; s null, 49 or 59: T_GRID and S_GRID in
    # graders/claim_judge.py). The verdict judge reads neither.
    support_threshold: int = Field(default=69, ge=0, le=100)
    skeptic_threshold: int | None = Field(default=None, ge=0, le=100)
    # Judge v6 (docs/plans/judge-v6.md, change 1): the claim judge asks for a gap type and a term
    # per claim, and two of the types fail a claim at every t. Off, its prompts and tools are
    # judge v5's. Part of the version. The verdict judge reads it not at all.
    gap_types: bool = False
    # Whether the verdict judge also asks if the answer responds to the question, which needs a
    # gold answer. The claim judge never asks it, so its config says false.
    responsive: bool = True


class SuitesConfig(_Strict):
    # Services deliberately kept out of the snapshot so unanswerable cases have real material.
    # `evals validate` errors if one of them shows up in a page title or URL (ADR 0006).
    held_out_services: list[str] = Field(default_factory=list)


class ProviderConfig(_Strict):
    """A provider behind the OpenAI-compatible API (harness/openai_compat.py)."""

    name: str
    base_url: str
    api_key_env: str  # the environment variable that holds the key, never the key
    model: str
    pricing: Pricing = Pricing()
    timeout_s: float = 30.0
    max_tokens_field: str = "max_completion_tokens"  # some servers want "max_tokens"


class GatewayConfig(_Strict):
    """Failover (harness/gateway.py). With no fallback, it is one provider and a breaker."""

    primary_name: str = "anthropic"
    primary_timeout_s: float = 30.0
    primary_retries: int = 1  # kept low: a failover is faster than waiting out long retries
    failure_threshold: int = 3  # failures in a row that open a provider's circuit
    cooldown_s: float = 30.0
    fallback: ProviderConfig | None = None


class ServiceConfig(_Strict):
    """The docs agent as a service (docs/plans/phase-8-llm-ops.md)."""

    host: str = "127.0.0.1"  # localhost only: the service has no authentication
    port: int = 8000
    # Answers worked on at once. Past this a request is refused at once: a queue in front of a
    # slow backend turns a short overload into a long one.
    max_concurrent: int = 8
    retry_after_s: int = 5  # what a refused client is told to wait
    answer_timeout_s: float = 120.0
    max_question_chars: int = 2000
    daily_budget_usd: float = 1.0  # past this the service refuses questions until the next UTC day
    spend_path: Path = Path("results/service/spend.json")
    registry_path: Path = Path("configs/registry.yaml")
    registry_log: Path = Path("results/service/registry-log.jsonl")
    gateway: GatewayConfig = GatewayConfig()
    # Set, the service runs on a fake provider at about this latency per model call, for a free
    # load test (service/fake.py). Every answer then reports the provider `fake`.
    fake_latency_s: float | None = None
    # The release drills' prompts are known only when this is set (service/drills.py).
    drill: bool = False


class Config(_Strict):
    paths: Paths = Paths()
    run: RunConfig = RunConfig()
    docs_agent: DocsAgentConfig = DocsAgentConfig()
    graders: GradersConfig = GradersConfig()
    judge: JudgeConfig = JudgeConfig()
    suites: SuitesConfig = SuitesConfig()
    service: ServiceConfig = ServiceConfig()


def load_config(path: Path | None = None) -> Config:
    """Load a YAML config file; missing keys fall back to the defaults above."""
    if path is None:
        return Config()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
