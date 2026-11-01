"""The real wiring of the service: the snapshot, the API client, the docs agent per prompt version.

Kept apart from `app.py` so that nothing there needs a snapshot or an API key. There is no call
cache here: the cache makes evals reproducible by replaying replies, which would freeze a served
answer.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.config import Config, Pricing, load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.gateway import CircuitBreaker, Provider, ResilientClient
from agent_evals.harness.llm import AnthropicClient
from agent_evals.harness.openai_compat import OpenAICompatibleClient
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.fake import FakeProvider
from agent_evals.service.metrics import ServiceMetrics
from agent_evals.service.registry import PromptRegistry
from agent_evals.service.tracing import make_tracer
from agent_evals.types import Agent

log = logging.getLogger("agent_evals.service")
FAKE = "fake"


def registry_of(config: Config) -> PromptRegistry:
    return PromptRegistry(config.service.registry_path, config.service.registry_log, SYSTEM_PROMPTS)


def gateway_of(config: Config, metrics: ServiceMetrics) -> ResilientClient:
    """The primary, and the fallback when one is configured and its key is set. With no key the
    service still runs, on one provider, and says so at startup."""
    settings = config.service.gateway

    def breaker() -> CircuitBreaker:
        return CircuitBreaker(settings.failure_threshold, settings.cooldown_s)

    if config.service.fake_latency_s is not None:
        fake = Provider(FAKE, FakeProvider(config.service.fake_latency_s), breaker())
        return ResilientClient([fake], on_event=metrics.on_gateway_event)
    primary = AnthropicClient(
        max_retries=settings.primary_retries, timeout_s=settings.primary_timeout_s
    )
    providers = [Provider(settings.primary_name, primary, breaker())]
    fallback = settings.fallback
    key = os.environ.get(fallback.api_key_env) if fallback is not None else None
    if fallback is not None and key:
        client = OpenAICompatibleClient(
            base_url=fallback.base_url,
            api_key=key,
            model=fallback.model,
            timeout_s=fallback.timeout_s,
            max_tokens_field=fallback.max_tokens_field,
        )
        providers.append(Provider(fallback.name, client, breaker()))
    elif fallback is not None:
        log.warning("no key in %s: serving with no fallback", fallback.api_key_env)
    return ResilientClient(providers, on_event=metrics.on_gateway_event)


def build_app(config_path: Path) -> FastAPI:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(config_path)
    corpus = Corpus.load(config.paths.corpus_dir)
    metrics = ServiceMetrics()
    llm = gateway_of(config, metrics)
    fallback = config.service.gateway.fallback
    prices = {FAKE: Pricing()} | ({fallback.name: fallback.pricing} if fallback else {})
    agents: dict[str, Agent] = {}
    lock = threading.Lock()

    def agent_for(version: str) -> Agent:
        with lock:
            if version not in agents:
                settings = config.docs_agent.model_copy(update={"prompt_version": version})
                agents[version] = DocsAgent(llm, corpus, settings)
            return agents[version]

    return create_app(
        Deps(
            settings=config.service,
            registry=registry_of(config),
            agent_for=agent_for,
            pricing=config.docs_agent.model.pricing,
            budget=DailyBudget(config.service.spend_path, config.service.daily_budget_usd),
            metrics=metrics,
            ready_checks={"snapshot": lambda: bool(corpus.pages), "provider": llm.available},
            provider_pricing=prices,
            tracer=make_tracer(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
        )
    )
