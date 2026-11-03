"""The AWS code agent: a Claude tool-use loop that writes boto3 code (phase 3, part 1).

Like the docs agent, it answers through a `submit_answer` tool whose input is schema-checked and
then held to content rules. Its tools look operations up in botocore's models and run code in the
sandbox against a fresh moto, set up as the task describes. The hidden test never runs inside the
loop: that is the grader's (docs/plans/phase-3-code-agent.md).
"""

from __future__ import annotations

import ast
import time
from collections.abc import Mapping
from importlib.metadata import version as package_version
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_evals.agents.code.lookup import LookupError_, describe_operation, list_operations
from agent_evals.agents.code.prompts import (
    CLOSED_BOOK_PROMPT_V1,
    INVALID_ANSWER_V1,
    NUDGE_V1,
    REJECT_ABSTAINED_WITH_CODE_V1,
    REJECT_NO_SOLVE_V1,
    REJECT_SYNTAX_V1,
    REJECTED_V1,
    RUN_LIMIT_V1,
    SYSTEM_PROMPTS,
)
from agent_evals.agents.code.sandbox import CodeRunner, Job
from agent_evals.agents.docs.agent import _sanitize
from agent_evals.config import CodeAgentConfig
from agent_evals.contracts import CodeAnswer
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.llm import LLMClient, usage_from
from agent_evals.types import AgentResult, Case, RunContext, Step, Usage

_TOOL_OUTPUT_LOG_CHARS = 2000


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(min_length=1)


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(min_length=1)
    operation: str = Field(min_length=1)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)


def submission_problems(answer: CodeAnswer) -> list[str]:
    """Content rules a schema-valid submission must still meet. Empty means accepted."""
    if answer.abstained:
        return [REJECT_ABSTAINED_WITH_CODE_V1] if answer.code.strip() else []
    try:
        tree = ast.parse(answer.code)
    except SyntaxError as exc:
        return [REJECT_SYNTAX_V1.format(error=f"line {exc.lineno}: {exc.msg}")]
    solves = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "solve" and n.args.args
    ]
    return [] if solves else [REJECT_NO_SOLVE_V1]


SUBMIT_SPEC: dict[str, Any] = {
    "name": "submit_answer",
    "description": "Submit your final answer. If the result is an error, fix it and submit again.",
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The whole module, defining solve(session). Empty if abstained.",
            },
            "abstained": {"type": "boolean"},
            "explanation": {
                "type": "string",
                "description": "What the code does, or what does not exist if abstained.",
            },
        },
        "required": ["code", "abstained", "explanation"],
        "additionalProperties": False,
    },
}


def tool_specs(strict: bool, *, lookups: bool = True) -> list[dict[str, Any]]:
    service = {"type": "string", "description": 'A boto3 service name, such as "s3" or "sqs".'}
    specs: list[dict[str, Any]] = []
    if lookups:
        specs += [
            {
                "name": "list_operations",
                "description": "List a service's operations, as boto3 client method names.",
                "input_schema": {
                    "type": "object",
                    "properties": {"service": service},
                    "required": ["service"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "describe_operation",
                "description": (
                    "Describe an operation's parameters, with * marking required ones, nested "
                    "structures and allowed values, and the keys of what it returns."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "service": service,
                        "operation": {"type": "string", "description": "A client method name."},
                    },
                    "required": ["service", "operation"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "run_code",
                "description": (
                    "Run a module defining solve(session) in a sandbox account set up as the task "
                    "describes. Returns what it printed, what solve returned, and any error."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        ]
    specs.append(dict(SUBMIT_SPEC))
    if strict:
        for spec in specs:
            spec["strict"] = True
    return specs


class CodeAgent:
    name = "code"
    lookups = True  # the closed-book baseline has none

    def __init__(
        self,
        llm: LLMClient,
        runner: CodeRunner,
        config: CodeAgentConfig,
        prompts: Mapping[str, str] = SYSTEM_PROMPTS,
    ) -> None:
        self.llm = llm
        self.runner = runner
        self.config = config
        if config.prompt_version not in prompts:
            raise ValueError(
                f"unknown code_agent.prompt_version {config.prompt_version!r}; "
                f"known: {sorted(prompts)}"
            )
        self.system_prompt = self._system_prompt(prompts[config.prompt_version])
        self.tools = tool_specs(config.strict_tools, lookups=self.lookups)

    def _system_prompt(self, chosen: str) -> str:
        return chosen

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.config.model.name,
            "prompt_sha256": sha256_text(self.system_prompt)[:12],
            "max_steps": self.config.max_steps,
            "botocore": package_version("botocore"),
            "runner": getattr(self.runner, "tag", type(self.runner).__name__),
        }

    @property
    def version(self) -> str:
        """Changes whenever anything that can change behavior changes: every setting, every text
        the model reads, the tools, the service models it looks up, and the sandbox it runs in."""
        fingerprint = {
            "agent": self.name,
            "config": self.config.model_dump(mode="json", exclude={"model": {"pricing"}}),
            "prompts": {
                "system": self.system_prompt,
                "nudge": NUDGE_V1,
                "invalid_answer": INVALID_ANSWER_V1,
                "rejected": REJECTED_V1,
                "reject_no_solve": REJECT_NO_SOLVE_V1,
                "reject_syntax": REJECT_SYNTAX_V1,
                "reject_abstained_with_code": REJECT_ABSTAINED_WITH_CODE_V1,
                "run_limit": RUN_LIMIT_V1,
            },
            "tools": self.tools,
            "botocore": package_version("botocore"),
            "runner": getattr(self.runner, "tag", type(self.runner).__name__),
        }
        return sha256_text(canonical_json(fingerprint))[:12]

    # ---- tools ----------------------------------------------------------------------

    def _call_tool(self, name: str, raw: dict[str, Any], case: Case, runs: int) -> tuple[str, bool]:
        """Return (result text, is_error). Errors go back to the model so it can recover."""
        try:
            if name == "list_operations":
                return list_operations(ServiceInput.model_validate(raw).service), False
            if name == "describe_operation":
                op = OperationInput.model_validate(raw)
                limit = self.config.describe_chars
                return describe_operation(op.service, op.operation, limit), False
            if name == "run_code":
                code = RunInput.model_validate(raw).code
                if runs >= self.config.max_runs:
                    return RUN_LIMIT_V1, True
                setup = case.task.setup if case.task is not None else ""
                run = self.runner.run(Job(code=code, setup=setup))
                text = (
                    f"ok: {run.ok}\nstage: {run.stage}\nerror: {run.error}\n"
                    f"returned: {run.result}\nprinted:\n{run.stdout}"
                )
                return text, not run.ok
        except LookupError_ as exc:
            return str(exc), True
        except ValidationError as exc:
            return f"Invalid input for {name}: {exc.errors(include_url=False)}", True
        return f"Unknown tool {name!r}.", True

    def _check_submission(self, raw: dict[str, Any]) -> tuple[CodeAnswer | None, str, bool]:
        try:
            candidate = CodeAnswer.model_validate(raw)
        except ValidationError as exc:
            return None, INVALID_ANSWER_V1.format(errors=exc.errors(include_url=False)), True
        problems = submission_problems(candidate)
        if problems:
            return None, REJECTED_V1.format(problems=" ".join(problems)), True
        return candidate, "Answer received.", False

    # ---- loop -----------------------------------------------------------------------

    def run(self, case: Case, ctx: RunContext) -> AgentResult:
        cfg = self.config
        messages: list[dict[str, Any]] = [{"role": "user", "content": case.question}]
        steps: list[Step] = []
        usage = Usage()
        nudged = False
        rejections = 0
        runs = 0
        last_rejection = ""

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
            usage = usage.add(usage_from(resp))
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
                messages.append({"role": "user", "content": NUDGE_V1})
                nudged = True
                continue

            results: list[dict[str, Any]] = []
            submission: CodeAnswer | None = None
            for tu in tool_uses:
                start = time.perf_counter()
                if tu["name"] == "submit_answer":
                    candidate, text, is_error = self._check_submission(tu["input"])
                    if is_error:
                        rejections += 1
                        last_rejection = text
                    else:
                        submission = candidate
                else:
                    text, is_error = self._call_tool(tu["name"], tu["input"], case, runs)
                    runs += tu["name"] == "run_code"
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
            if rejections >= cfg.max_submission_rejections:
                return done(error=f"malformed_submission: {last_rejection}")
            messages.append({"role": "user", "content": results})
        return done(error="max_steps")


class ClosedBookCodeAgent(CodeAgent):
    """The baseline: the same model and contract, with no lookups and no runs."""

    name = "code-closed-book"
    lookups = False

    def _system_prompt(self, chosen: str) -> str:
        return CLOSED_BOOK_PROMPT_V1
