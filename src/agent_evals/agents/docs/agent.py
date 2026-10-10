"""AWS docs agent: a Claude tool-use loop that must answer with cited, verbatim quotes.

The final answer arrives through a `submit_answer` tool whose input is schema-checked, so the
harness gets structured output it can grade with code (see docs/adr/0002). A schema-valid
submission must also pass `submission_problems`; failures go back to the model as tool errors.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_evals.agents.docs.prompts import REJECT_MARKUP_V1, REJECT_UNCITED_V1, SYSTEM_PROMPT_V1
from agent_evals.config import DocsAgentConfig
from agent_evals.corpus import Corpus
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.llm import LLMClient, LLMResponse
from agent_evals.tool_markup import find_tool_markup
from agent_evals.types import AgentResult, Case, RunContext, Step, Usage

NUDGE = (
    "You have not submitted an answer yet. Call submit_answer now, or set abstained to true "
    "if the documentation does not answer the question."
)
_TOOL_OUTPUT_LOG_CHARS = 2000


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    quote: str


class DocsAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    citations: list[Citation]
    abstained: bool
    confidence: float = Field(ge=0.0, le=1.0)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    start: int = Field(default=0, ge=0)


def submission_problems(answer: DocsAnswer) -> list[str]:
    """Content rules a schema-valid submission must still meet. An empty list means accepted."""
    problems: list[str] = []
    snippet = find_tool_markup(answer.answer)
    if snippet is not None:
        problems.append(REJECT_MARKUP_V1.format(snippet=snippet))
    if not answer.abstained and not answer.citations:
        problems.append(REJECT_UNCITED_V1)
    return problems


def tool_specs(strict: bool) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {
            "name": "search_docs",
            "description": (
                "Keyword search over the AWS documentation snapshot. Returns page URLs, "
                "section headings and short snippets. Snippets are not enough to cite: read "
                "the page first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search keywords."}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_page",
            "description": (
                "Read a documentation page from the snapshot as markdown. Long pages are "
                "returned in parts; pass the start offset from the truncation note to continue."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "A URL returned by search_docs."},
                    "start": {"type": "integer", "description": "Character offset; use 0 first."},
                },
                "required": ["url", "start"],
                "additionalProperties": False,
            },
        },
        {
            "name": "submit_answer",
            "description": "Submit your final answer. Call exactly once, at the end.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "quote": {
                                    "type": "string",
                                    "description": "Copied word for word from the page.",
                                },
                            },
                            "required": ["url", "quote"],
                            "additionalProperties": False,
                        },
                    },
                    "abstained": {"type": "boolean"},
                    "confidence": {"type": "number", "description": "Between 0 and 1."},
                },
                "required": ["answer", "citations", "abstained", "confidence"],
                "additionalProperties": False,
            },
        },
    ]
    if strict:
        for spec in specs:
            spec["strict"] = True
    return specs


def _sanitize(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the fields the API accepts back in an assistant turn."""
    clean: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") == "text":
            clean.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "tool_use":
            clean.append(
                {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input", {})}
            )
        else:
            clean.append(b)  # e.g. thinking blocks must be passed back unchanged
    return clean


def _usage_of(resp: LLMResponse) -> Usage:
    return Usage(
        input_tokens=int(resp.usage.get("input_tokens", 0)),
        output_tokens=int(resp.usage.get("output_tokens", 0)),
        model_calls=1,
        cached_calls=1 if resp.cache_hit else 0,
        latency_ms=resp.latency_ms,
    )


class DocsAgent:
    name = "docs"

    def __init__(self, llm: LLMClient, corpus: Corpus, config: DocsAgentConfig) -> None:
        self.llm = llm
        self.corpus = corpus
        self.config = config
        self.system_prompt = SYSTEM_PROMPT_V1
        self.tools = tool_specs(config.strict_tools)

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.config.model.name,
            "prompt_sha256": sha256_text(self.system_prompt)[:12],
            "max_steps": self.config.max_steps,
            "search_k": self.config.search_k,
        }

    @property
    def version(self) -> str:
        """Changes whenever anything that can change behavior changes."""
        fingerprint = {
            "model": self.config.model.model_dump(exclude={"pricing"}),
            "prompt": self.system_prompt,
            "rejections": [REJECT_UNCITED_V1, REJECT_MARKUP_V1],
            "tools": self.tools,
            "max_steps": self.config.max_steps,
            "search_k": self.config.search_k,
            "page_chars": self.config.page_chars,
        }
        return sha256_text(canonical_json(fingerprint))[:12]

    # ---- tools ----------------------------------------------------------------------

    def _call_tool(self, name: str, raw: dict[str, Any]) -> tuple[str, bool]:
        """Return (result text, is_error). Errors go back to the model so it can recover."""
        try:
            if name == "search_docs":
                q = SearchInput.model_validate(raw)
                hits = self.corpus.search(q.query, k=self.config.search_k)
                if not hits:
                    return "No results. Try different keywords.", False
                return "\n\n".join(
                    f"[{i}] {h.title} > {h.heading}\nURL: {h.url}\n{h.snippet}"
                    for i, h in enumerate(hits, 1)
                ), False
            if name == "read_page":
                r = ReadInput.model_validate(raw)
                text = self.corpus.read(r.url, r.start, self.config.page_chars)
                if text is None:
                    return f"No page with URL {r.url} in the snapshot.", True
                return text, False
        except ValidationError as exc:
            return f"Invalid input for {name}: {exc.errors(include_url=False)}", True
        return f"Unknown tool {name!r}.", True

    # ---- loop -----------------------------------------------------------------------

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        cfg = self.config
        messages: list[dict[str, Any]] = [{"role": "user", "content": case.question}]
        steps: list[Step] = []
        usage = Usage()
        nudged = False

        def done(output: dict[str, Any] | None = None, error: str | None = None) -> AgentResult:
            return AgentResult(output=output or {}, trajectory=steps, usage=usage, error=error)

        for _ in range(cfg.max_steps):
            request: dict[str, Any] = {
                "model": cfg.model.name,
                "max_tokens": cfg.model.max_tokens,
                "system": self.system_prompt,
                "tools": self.tools,
                "messages": messages,
            }
            if cfg.model.temperature is not None:
                request["temperature"] = cfg.model.temperature

            resp = self.llm.create(request, salt=ctx.cache_salt)
            usage = usage.add(_usage_of(resp))
            blocks = _sanitize(resp.content)
            steps.append(
                Step(
                    index=len(steps),
                    kind="model",
                    name=cfg.model.name,
                    output={"stop_reason": resp.stop_reason, "content": blocks},
                    latency_ms=resp.latency_ms,
                    cache_hit=resp.cache_hit,
                )
            )
            messages.append({"role": "assistant", "content": blocks})

            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                if resp.stop_reason == "max_tokens":
                    return done(error="max_tokens")
                if nudged:
                    return done(error="no_submission")
                messages.append({"role": "user", "content": NUDGE})
                nudged = True
                continue

            results: list[dict[str, Any]] = []
            submission: DocsAnswer | None = None
            for tu in tool_uses:
                start = time.perf_counter()
                if tu["name"] == "submit_answer":
                    try:
                        candidate = DocsAnswer.model_validate(tu["input"])
                    except ValidationError as exc:
                        text = f"Invalid answer: {exc.errors(include_url=False)}. Fix and resubmit."
                        is_error = True
                    else:
                        problems = submission_problems(candidate)
                        if problems:
                            text = "Rejected: " + " ".join(problems) + " Fix and resubmit."
                            is_error = True
                        else:
                            submission, text, is_error = candidate, "Answer received.", False
                else:
                    text, is_error = self._call_tool(tu["name"], tu["input"])
                steps.append(
                    Step(
                        index=len(steps),
                        kind="tool",
                        name=tu["name"],
                        input=tu["input"],
                        output=text[:_TOOL_OUTPUT_LOG_CHARS],
                        is_error=is_error,
                        latency_ms=(time.perf_counter() - start) * 1000,
                    )
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": text,
                        "is_error": is_error,
                    }
                )

            if submission is not None:
                return done(output=submission.model_dump())
            messages.append({"role": "user", "content": results})

        return done(error="max_steps")
