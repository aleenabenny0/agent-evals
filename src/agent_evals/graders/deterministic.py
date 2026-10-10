"""Tier-1 graders: plain code, no LLM. Cheap, exact, and run on every attempt.

An LLM judge (tier 3) is added in the Measurement phase for answers that keywords can't
settle; these checks stay as the first line because they never drift.
"""

from __future__ import annotations

from agent_evals.corpus import Corpus, normalize
from agent_evals.tool_markup import find_tool_markup
from agent_evals.types import AgentResult, Case, Grade

MIN_QUOTE_CHARS = 12


def _abstained(result: AgentResult) -> bool:
    return bool(result.output.get("abstained", False))


class AnswerGrader:
    """Correctness by required phrases, plus abstention behavior."""

    name = "answer_keywords"
    version = "1"

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error:
            return [
                Grade(
                    metric="correct", value=0.0, passed=False, reason=f"agent error: {result.error}"
                )
            ]
        abstained = _abstained(result)
        grades = [Grade(metric="abstained", value=float(abstained))]

        if not case.answerable:
            grades.append(
                Grade(
                    metric="correct",
                    value=float(abstained),
                    passed=abstained,
                    reason="" if abstained else "answered an unanswerable question",
                )
            )
            grades.append(Grade(metric="missed_abstention", value=float(not abstained)))
            return grades

        grades.append(Grade(metric="wrong_abstention", value=float(abstained)))
        if abstained:
            grades.append(
                Grade(
                    metric="correct",
                    value=0.0,
                    passed=False,
                    reason="abstained on an answerable question",
                )
            )
            return grades

        answer = normalize(str(result.output.get("answer", "")))
        missing = [
            group
            for group in case.gold.must_include
            if not any(normalize(alt) in answer for alt in group)
        ]
        ok = not missing
        reason = "" if ok else "missing: " + "; ".join(" | ".join(g) for g in missing)
        grades.append(Grade(metric="correct", value=float(ok), passed=ok, reason=reason))
        return grades


class CitationGrader:
    """Every citation must point at a snapshot page and quote it verbatim."""

    name = "citations"
    version = "1"

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error:
            return []
        citations = result.output.get("citations", []) or []
        if not citations:
            if _abstained(result):
                return []
            return [
                Grade(
                    metric="uncited_answer",
                    value=1.0,
                    passed=False,
                    reason="answer has no citations",
                )
            ]

        problems: list[str] = []
        for c in citations:
            url, quote = str(c.get("url", "")), str(c.get("quote", ""))
            if url not in self.corpus.pages:
                problems.append(f"{url}: page not in snapshot")
            elif len(normalize(quote)) < MIN_QUOTE_CHARS:
                problems.append(f"{url}: quote too short to verify")
            elif not self.corpus.contains_quote(url, quote):
                problems.append(f"{url}: quote not found on page: {quote[:80]!r}")

        valid = len(citations) - len(problems)
        return [
            Grade(metric="uncited_answer", value=0.0, passed=True),
            Grade(metric="citation_valid_rate", value=valid / len(citations)),
            Grade(
                metric="fabricated_citation",
                value=float(bool(problems)),
                passed=not problems,
                reason="; ".join(problems),
            ),
        ]


class FormatGrader:
    """The answer must be plain prose: no tool-call markup leaked into it.

    Scope is the `answer` field only. Citation quotes may legitimately contain XML, and the
    shape of the output is checked by the agent's schema, not here.
    """

    name = "output_format"
    version = "2"  # 2: narrower, case-sensitive detector that ignores AWS placeholders and XML

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error:
            return []
        snippet = find_tool_markup(str(result.output.get("answer", "")))
        return [
            Grade(
                metric="malformed_output",
                value=float(snippet is not None),
                passed=snippet is None,
                reason="" if snippet is None else f"tool-call markup in answer: {snippet!r}",
            )
        ]


class TrajectoryGrader:
    """Retrieval and efficiency signals read from the trajectory."""

    name = "trajectory"
    version = "1"

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        tool_steps = [s for s in result.trajectory if s.kind == "tool"]
        model_steps = [s for s in result.trajectory if s.kind == "model"]
        grades = [
            Grade(metric="model_calls", value=float(len(model_steps))),
            Grade(metric="tool_errors", value=float(sum(s.is_error for s in tool_steps))),
        ]
        if case.gold.evidence:
            read = {
                str(s.input.get("url"))
                for s in tool_steps
                if s.name == "read_page" and not s.is_error and isinstance(s.input, dict)
            }
            cited = {str(c.get("url")) for c in result.output.get("citations", []) or []}
            found = sum(url in read | cited for url in case.gold.evidence)
            recall = found / len(case.gold.evidence)
            grades.append(Grade(metric="evidence_recall", value=recall, passed=recall == 1.0))
        return grades
