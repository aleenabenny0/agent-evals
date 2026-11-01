"""The real wiring of the service: the snapshot, the API client, the docs agent per prompt version.

Kept apart from `app.py` so that nothing there needs a snapshot or an API key. There is no call
cache here: the cache makes evals reproducible by replaying replies, which would freeze a served
answer.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.prompts import SYSTEM_PROMPTS
from agent_evals.config import Config, load_config
from agent_evals.corpus import Corpus
from agent_evals.harness.llm import AnthropicClient
from agent_evals.service.app import Deps, create_app
from agent_evals.service.budget import DailyBudget
from agent_evals.service.registry import PromptRegistry
from agent_evals.types import Agent


def registry_of(config: Config) -> PromptRegistry:
    return PromptRegistry(config.service.registry_path, config.service.registry_log, SYSTEM_PROMPTS)


def build_app(config_path: Path) -> FastAPI:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(config_path)
    corpus = Corpus.load(config.paths.corpus_dir)
    llm = AnthropicClient()
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
            ready_checks={"snapshot": lambda: bool(corpus.pages)},
        )
    )
