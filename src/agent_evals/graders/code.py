"""The code agent's graders, all code (docs/plans/phase-3-code-agent.md, part 2).

`CodeTaskGrader` runs the submitted code and the case's hidden test in the sandbox and scores
`correct`, with the abstention metrics the docs agent has, so the report and the gate read both
agents alike. `CodeApiGrader` checks the submitted code against botocore's models without running
it. Neither runs for an attempt that errored, beyond scoring it incorrect.
"""

from __future__ import annotations

from agent_evals.agents.code.sandbox import CodeRunner, Job
from agent_evals.graders.api_check import check_api
from agent_evals.types import AgentResult, Case, Grade


def _abstained(result: AgentResult) -> bool:
    return bool(result.output.get("abstained", False))


class CodeTaskGrader:
    name = "code_task"
    version = "1"

    def __init__(self, runner: CodeRunner) -> None:
        self.runner = runner

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        task = case.task
        if task is None:
            raise ValueError(f"{case.id}: not a code case")
        if result.error:
            reason = f"agent error: {result.error}"
            return [Grade(metric="correct", value=0.0, passed=False, reason=reason)]
        abstained = _abstained(result)
        grades = [Grade(metric="abstained", value=float(abstained))]
        if not case.answerable:
            grades.append(Grade(metric="missed_abstention", value=float(not abstained)))
            reason = "" if abstained else "wrote code for a task AWS does not support"
            grades.append(
                Grade(metric="correct", value=float(abstained), passed=abstained, reason=reason)
            )
            return grades
        grades.append(Grade(metric="wrong_abstention", value=float(abstained)))
        if abstained:
            grades.append(
                Grade(
                    metric="correct", value=0.0, passed=False, reason="abstained on a possible task"
                )
            )
            return grades
        run = self.runner.run(
            Job(code=str(result.output.get("code", "")), setup=task.setup, test=task.test)
        )
        grades.append(Grade(metric="sandbox_seconds", value=run.seconds))
        if run.case_defect:  # the machine checks make this rare; it is the case's fault, flagged
            grades.append(
                Grade(metric="case_defect", value=1.0, passed=False, reason=str(run.error))
            )
        reason = "" if run.ok else f"failed at {run.stage}: {run.error}"
        grades.append(Grade(metric="correct", value=float(run.ok), passed=run.ok, reason=reason))
        return grades


class CodeApiGrader:
    name = "code_api"
    version = "1"

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error or _abstained(result) or case.task is None:
            return []
        report = check_api(str(result.output.get("code", "")))
        if report.syntax_error is not None:
            return [
                Grade(
                    metric="api_syntax_error", value=1.0, passed=False, reason=report.syntax_error
                )
            ]

        def flag(metric: str, problems: list[str]) -> Grade:
            return Grade(
                metric=metric,
                value=float(bool(problems)),
                passed=not problems,
                reason="; ".join(problems),
            )

        unrequested = [op for op in report.destructive() if op not in case.task.destructive_ok]
        return [
            flag("hallucinated_operation", report.hallucinated_operations),
            flag("hallucinated_parameter", report.hallucinated_parameters),
            flag("missing_required_parameter", report.missing_required),
            flag("unrequested_destructive_call", unrequested),
            Grade(metric="api_calls", value=float(len(report.calls))),
            Grade(
                metric="unchecked_calls",
                value=float(len(report.unchecked)),
                reason="; ".join(report.unchecked),
            ),
        ]
