"""The HTTP service: one question in, one cited answer out, with limits around the agent.

The app knows the agent only through the `Agent` protocol, and is handed everything it needs
(`Deps`), so tests run it with a scripted agent and no API. An answer takes seconds and blocks on
the network, so it runs on its own pool of threads, one per slot of capacity: past that capacity a
request is refused at once rather than queued, because a queue in front of a slow backend turns a
short overload into a long one.

A timed-out answer cannot be cancelled mid-call, so its thread runs on and keeps its slot until it
finishes, and its spend is still counted. The slot is what keeps a slow provider from stacking up
work the service believes it has dropped.

Logs and metrics never hold a question's text: a log line has its hash and length.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import BaseModel, Field

from agent_evals.config import Pricing, ServiceConfig
from agent_evals.harness.cache import sha256_text
from agent_evals.harness.gateway import CURRENT_CALLS, CallRecord
from agent_evals.service.budget import DailyBudget
from agent_evals.service.metrics import ServiceMetrics
from agent_evals.service.registry import Assignment, PromptRegistry, assign
from agent_evals.service.tracing import record_steps
from agent_evals.types import Agent, AgentResult, Case, RunContext

log = logging.getLogger("agent_evals.service")


class AskRequest(BaseModel):
    question: str = Field(min_length=1)


class Citation(BaseModel):
    url: str
    quote: str


class AskResponse(BaseModel):
    request_id: str
    answer: str
    citations: list[Citation]
    abstained: bool
    prompt_version: str
    role: str
    agent_version: str
    providers: list[str]  # who answered, in order; more than one means a failover happened
    model_calls: int
    cost_usd: float | None
    latency_ms: float


@dataclass(frozen=True)
class Deps:
    """What the app needs from outside. `agent_for` gives the agent for a prompt version."""

    settings: ServiceConfig
    registry: PromptRegistry
    agent_for: Callable[[str], Agent]
    pricing: Pricing
    budget: DailyBudget
    metrics: ServiceMetrics = field(default_factory=ServiceMetrics)
    ready_checks: Mapping[str, Callable[[], bool]] = field(default_factory=dict)
    # Prices by provider name, when a gateway reports who served each call. Any other call is
    # priced at `pricing`.
    provider_pricing: Mapping[str, Pricing] = field(default_factory=dict)
    tracer: Tracer = field(default_factory=trace.NoOpTracer)


@dataclass(frozen=True)
class Answered:
    result: AgentResult
    calls: list[CallRecord]


def _answer(agent: Agent, case: Case, context: RunContext) -> Answered:
    """One agent run, with a record of who served its model calls. Runs on an answer's thread, so
    the record is that answer's alone."""
    calls: list[CallRecord] = []
    token = CURRENT_CALLS.set(calls)
    try:
        return Answered(agent.run(case, context), calls)
    finally:
        CURRENT_CALLS.reset(token)


def _providers(calls: Sequence[CallRecord]) -> list[str]:
    """Who served the calls, each once, in the order they first did."""
    return list(dict.fromkeys(call.provider for call in calls))


def _case(request_id: str, question: str) -> Case:
    """A question as the agent takes it. It belongs to no suite, so it has no gold answer."""
    return Case(
        id=request_id,
        suite="online",
        agent="docs",
        type="lookup",
        question=question,
        split="online",
        provenance="simulated",
    )


def _citations(output: Mapping[str, Any]) -> list[Citation]:
    raw = output.get("citations")
    held = raw if isinstance(raw, list) else []
    return [
        Citation(url=str(c.get("url", "")), quote=str(c.get("quote", "")))
        for c in held
        if isinstance(c, dict)
    ]


def create_app(deps: Deps) -> FastAPI:
    settings, metrics = deps.settings, deps.metrics
    pool = ThreadPoolExecutor(max_workers=settings.max_concurrent, thread_name_prefix="answer")
    slots = threading.BoundedSemaphore(settings.max_concurrent)
    metrics.capacity.set(settings.max_concurrent)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="agent-evals docs agent", version="1", lifespan=lifespan)

    def refused(
        status: int,
        outcome: str,
        message: str,
        request_id: str,
        assigned: Assignment | None = None,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        version = assigned.version if assigned else "none"
        role = assigned.role if assigned else "none"
        metrics.requests.labels(outcome, version, role).inc()
        span = trace.get_current_span()
        span.set_attributes({"request.id": request_id, "outcome": outcome})
        if status >= 500:
            span.set_status(Status(StatusCode.ERROR, outcome))
        log.info(json.dumps({"request_id": request_id, "outcome": outcome, "status": status}))
        body = {"error": outcome, "message": message, "request_id": request_id}
        return JSONResponse(body, status_code=status, headers=headers)

    def cost_of(answered: Answered) -> float | None:
        """What an answer cost, each call at its own provider's prices."""
        if not answered.calls:
            return answered.result.usage.cost_usd(deps.pricing)
        total = 0.0
        for call in answered.calls:
            pricing = deps.provider_pricing.get(call.provider, deps.pricing)
            cost = call.usage.cost_usd(pricing)
            if cost is None:
                return None
            total += cost
        return total

    def settle(assigned: Assignment, started: float, future: Future[Answered]) -> None:
        """Runs when an answer's thread ends, in time or not: free the slot, count what it spent."""
        slots.release()
        metrics.in_flight.dec()
        metrics.latency.labels(assigned.version, assigned.role).observe(
            time.perf_counter() - started
        )
        if future.cancelled() or future.exception() is not None:
            return
        answered = future.result()
        result = answered.result
        usage, version = result.usage, assigned.version
        metrics.tokens.labels("input", version).inc(usage.input_tokens)
        metrics.tokens.labels("output", version).inc(usage.output_tokens)
        metrics.model_calls.labels(version).inc(usage.model_calls)
        metrics.tool_errors.labels(version).inc(
            sum(s.kind == "tool" and s.is_error for s in result.trajectory)
        )
        cost = cost_of(answered)
        if cost is not None:
            metrics.cost.labels(version).inc(cost)
            deps.budget.add(cost)

    @app.exception_handler(RequestValidationError)
    def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return refused(422, "invalid", "the body must be JSON with a question", uuid.uuid4().hex)

    @app.post("/v1/ask", response_model=AskResponse)
    def ask(body: AskRequest) -> AskResponse | JSONResponse:
        with deps.tracer.start_as_current_span("ask"):
            return answer_question(body)

    def answer_question(body: AskRequest) -> AskResponse | JSONResponse:
        request_id = uuid.uuid4().hex
        question = body.question
        if len(question) > settings.max_question_chars:
            limit = settings.max_question_chars
            return refused(422, "invalid", f"a question is at most {limit} characters", request_id)
        if deps.budget.exhausted():
            return refused(503, "over_budget", "the daily spend cap is reached", request_id)
        if not slots.acquire(blocking=False):
            headers = {"Retry-After": str(settings.retry_after_s)}
            return refused(429, "overloaded", "at capacity", request_id, headers=headers)
        state = deps.registry.state()
        metrics.registry_stale.set(0 if deps.registry.error is None else 1)
        assigned = assign(state, request_id)
        try:
            agent = deps.agent_for(assigned.version)
        except Exception:
            slots.release()
            log.exception("no agent for %s", assigned.version)
            return refused(500, "provider_error", "the agent is unavailable", request_id, assigned)
        started = time.perf_counter()
        started_ns = time.time_ns()
        metrics.in_flight.inc()
        context = RunContext(run_id="service", case_id=request_id, attempt=1)
        future = pool.submit(_answer, agent, _case(request_id, question), context)
        future.add_done_callback(lambda done: settle(assigned, started, done))
        try:
            answered = future.result(timeout=settings.answer_timeout_s)
        except FutureTimeout:
            return refused(504, "timeout", "the answer took too long", request_id, assigned)
        except Exception as exc:  # an API failure the client gave up on
            kind = type(exc).__name__
            log.info(json.dumps({"request_id": request_id, "exception": kind}))
            return refused(502, "provider_error", "the model provider failed", request_id, assigned)
        latency_ms = (time.perf_counter() - started) * 1000
        result = answered.result
        record_steps(deps.tracer, started_ns, result, answered.calls)
        trace.get_current_span().set_attributes(
            {
                "prompt.version": assigned.version,
                "prompt.role": assigned.role,
                "question.chars": len(question),
                "llm.model_calls": result.usage.model_calls,
            }
        )
        if result.error:
            return refused(502, "agent_failed", "no answer was produced", request_id, assigned)
        abstained = bool(result.output.get("abstained", False))
        outcome = "abstained" if abstained else "answered"
        metrics.requests.labels(outcome, assigned.version, assigned.role).inc()
        cost = cost_of(answered)
        providers = _providers(answered.calls)
        trace.get_current_span().set_attributes({"request.id": request_id, "outcome": outcome})
        log.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "question_sha256": sha256_text(question)[:12],
                    "question_chars": len(question),
                    "outcome": outcome,
                    "prompt_version": assigned.version,
                    "role": assigned.role,
                    "providers": providers,
                    "model_calls": result.usage.model_calls,
                    "latency_ms": round(latency_ms, 1),
                    "cost_usd": cost,
                }
            )
        )
        return AskResponse(
            request_id=request_id,
            answer=str(result.output.get("answer", "")),
            citations=_citations(result.output),
            abstained=abstained,
            prompt_version=assigned.version,
            role=assigned.role,
            agent_version=agent.version,
            providers=providers,
            model_calls=result.usage.model_calls,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "up"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        deps.registry.state()
        checks = {name: check() for name, check in deps.ready_checks.items()}
        checks["registry_file"] = deps.registry.error is None
        checks["budget_left"] = not deps.budget.exhausted()
        ready = all(checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    return app
