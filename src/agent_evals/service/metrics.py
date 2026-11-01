"""The service's Prometheus metrics: the four golden signals, and what is particular to an LLM.

Labels hold a prompt version, a role or an outcome from a short fixed list, never free text, so
the number of series stays small and no question can reach a metric.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# An answer takes seconds, not milliseconds, so the default buckets would not resolve it.
LATENCY_BUCKETS = (0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144)
OUTCOMES = (
    "answered",
    "abstained",
    "agent_failed",
    "provider_error",
    "timeout",
    "overloaded",
    "over_budget",
    "invalid",
)


class ServiceMetrics:
    """Each app has its own registry, so tests and a second app in one process stay apart."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "ask_requests",
            "Questions received, by how they ended.",
            ["outcome", "prompt_version", "role"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "ask_latency_seconds",
            "Time to answer, for the questions an agent ran.",
            ["prompt_version", "role"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "ask_in_flight", "Answers being worked on now.", registry=self.registry
        )
        self.capacity = Gauge(
            "ask_capacity", "The most answers worked on at once.", registry=self.registry
        )
        self.tokens = Counter(
            "llm_tokens", "Tokens spent.", ["kind", "prompt_version"], registry=self.registry
        )
        self.model_calls = Counter(
            "llm_model_calls", "Model calls made.", ["prompt_version"], registry=self.registry
        )
        self.cost = Counter(
            "llm_cost_usd",
            "Spend at the configured prices.",
            ["prompt_version"],
            registry=self.registry,
        )
        self.tool_errors = Counter(
            "agent_tool_errors",
            "Tool calls that came back as errors, rejected submissions included.",
            ["prompt_version"],
            registry=self.registry,
        )
        self.registry_stale = Gauge(
            "registry_file_bad",
            "1 while the registry file on disk is unusable and the last good state is in force.",
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
