"""Traces of each answer, built from the same trajectory the evals read.

The agent records every model call and tool call in its trajectory, with their latencies, so the
spans are written from that afterwards instead of threading a tracer through the agent. Nothing
about tracing reaches the agent, and a trace cannot disagree with what the evals see.

Spans hold names, counts and timings, never a question or an answer. A search query or a quote is
derived from the question, so step inputs and outputs stay out of traces too.
"""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode, Tracer

from agent_evals.harness.gateway import CallRecord
from agent_evals.types import AgentResult

SERVICE_NAME = "docs-agent"


def make_tracer(endpoint: str | None) -> Tracer:
    """A tracer that exports to an OTLP endpoint, or does nothing with none."""
    if not endpoint:
        return trace.NoOpTracer()
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer("agent_evals.service")


def record_steps(
    tracer: Tracer, started_ns: int, result: AgentResult, calls: Sequence[CallRecord]
) -> None:
    """One span per step of the trajectory, children of the current span, laid end to end from
    their recorded latencies. Each model step is paired with the call that served it."""
    at = started_ns
    served = iter(calls)
    for step in result.trajectory:
        length_ns = int(step.latency_ms * 1_000_000)
        span = tracer.start_span(f"{step.kind} {step.name}", start_time=at)
        span.set_attribute("agent.step", step.index)
        if step.kind == "model":
            call = next(served, None)
            if call is not None:
                span.set_attribute("llm.provider", call.provider)
                span.set_attribute("llm.input_tokens", call.usage.input_tokens)
                span.set_attribute("llm.output_tokens", call.usage.output_tokens)
        if step.is_error:
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=at + length_ns)
        at += length_ns
