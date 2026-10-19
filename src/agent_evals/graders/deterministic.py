"""Tier-1 graders: plain code, no LLM. Cheap, exact, and run on every attempt.

An LLM judge (tier 3) is added in the Measurement phase for answers that keywords can't
settle; these checks stay as the first line because they never drift.
"""

from __future__ import annotations

import json
from typing import Any

from agent_evals.corpus import Corpus, normalize, phrase_in
from agent_evals.entities import normalize_url, numbers_in, urls_in
from agent_evals.tool_markup import find_tool_markup
from agent_evals.types import AgentResult, Case, Grade


def _abstained(result: AgentResult) -> bool:
    return bool(result.output.get("abstained", False))


def _citations(result: AgentResult) -> list[Any]:
    """The citations list, or [] when the output has none or the field is not a list."""
    cits = result.output.get("citations")
    return cits if isinstance(cits, list) else []


def _pages_read(result: AgentResult) -> set[str]:
    """URLs of every page the agent read successfully."""
    return {
        str(s.input.get("url"))
        for s in result.trajectory
        if s.kind == "tool"
        and s.name == "read_page"
        and not s.is_error
        and isinstance(s.input, dict)
    }


def _pages_cited(result: AgentResult) -> set[str]:
    return {str(c.get("url")) for c in _citations(result) if isinstance(c, dict)}


class AnswerGrader:
    """Correctness by required phrases, plus abstention behavior. An online case has no
    reference answer, so it gets only `abstained`: never `correct`, `wrong_abstention` or
    `missed_abstention`, which would need one."""

    name = "answer_keywords"
    version = "3"  # 3: online cases record only abstained; 2: whole-word phrase matching

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if case.is_online:
            if result.error:
                return []
            return [Grade(metric="abstained", value=float(_abstained(result)))]
        if case.gold is None:  # Case validation makes this impossible offline
            raise ValueError(f"{case.id}: an offline case without gold")
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
            if not any(phrase_in(answer, alt) for alt in group)
        ]
        ok = not missing
        reason = "" if ok else "missing: " + "; ".join(" | ".join(g) for g in missing)
        grades.append(Grade(metric="correct", value=float(ok), passed=ok, reason=reason))
        return grades


class CitationGrader:
    """Every citation must point at a snapshot page and quote it verbatim."""

    name = "citations"
    version = "2"  # 2: quote threshold from config; malformed entries graded, not raised

    def __init__(self, corpus: Corpus, *, min_quote_chars: int) -> None:
        self.corpus = corpus
        self.min_quote_chars = min_quote_chars

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error:
            return []
        citations = _citations(result)
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
            if not isinstance(c, dict):
                problems.append(f"{c!r}: citation is not an object")
                continue
            url, quote = str(c.get("url", "")), str(c.get("quote", ""))
            if url not in self.corpus.pages:
                problems.append(f"{url}: page not in snapshot")
            elif len(normalize(quote)) < self.min_quote_chars:
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
    version = "3"  # 3: repeated_calls, evidence_precision

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        tool_steps = [s for s in result.trajectory if s.kind == "tool"]
        model_steps = [s for s in result.trajectory if s.kind == "model"]
        rejected = [s for s in tool_steps if s.name == "submit_answer" and s.is_error]
        # Loop detection: the same tool called again with exactly the same input. Key order in
        # the input does not matter. submit_answer counts too: resubmitting an identical
        # rejected answer is a loop.
        signatures = [
            (s.name, json.dumps(s.input, sort_keys=True, default=str)) for s in tool_steps
        ]
        grades = [
            Grade(metric="model_calls", value=float(len(model_steps))),
            Grade(metric="tool_errors", value=float(sum(s.is_error for s in tool_steps))),
            # Formatting reliability: how often the final answer had to be sent back, whether
            # or not the model recovered afterwards.
            Grade(metric="rejected_submissions", value=float(len(rejected))),
            Grade(metric="repeated_calls", value=float(len(signatures) - len(set(signatures)))),
        ]
        gold = case.gold  # absent on an online case: no evidence pages to score
        if gold is not None and gold.evidence:
            read = _pages_read(result)
            seen = read | _pages_cited(result)
            # One hit per group of alternatives; a group with any URL seen counts as found.
            found = sum(any(url in seen for url in group) for group in gold.evidence)
            recall = found / len(gold.evidence)
            grades.append(Grade(metric="evidence_recall", value=recall, passed=recall == 1.0))
            # Precision: of the distinct pages read, the share that were gold evidence. Recall
            # asks "did it find the right pages"; this asks "how much else did it read". Left
            # out when nothing was read (the oracle baseline is handed its pages): 0/0 is not
            # a rate.
            if read:
                on_target = len(read & set(gold.evidence_urls)) / len(read)
                grades.append(Grade(metric="evidence_precision", value=on_target))
        return grades


class GroundingGrader:
    """Every number and URL in the answer must appear somewhere the agent could have read it.

    Sources: the question, the pages the agent read, and the pages it cited. A figure on none of
    them was either invented or converted (a page's "900 seconds" written as "15 minutes"). The
    metric is called `unsupported`, not `fabricated`, because this check cannot tell the two
    apart; the judge's "supported" verdict settles conversions. Reference-free: it needs no gold
    answer, so it also scores live traffic (phase 7). Numbers written as words are not checked.
    """

    name = "grounding"
    version = "2"  # 2: list markers ("1.", "2)") and Region codes are not numbers

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error or _abstained(result):
            return []
        answer = str(result.output.get("answer", ""))
        urls = _pages_read(result) | _pages_cited(result)
        pages = [self.corpus.pages[u] for u in sorted(urls) if u in self.corpus.pages]
        seen_text = "\n".join([case.question, *(p.text for p in pages)])
        known_urls = urls_in(seen_text) | {normalize_url(p.url) for p in pages}
        problems: list[str] = []
        loose_numbers = sorted(numbers_in(answer) - numbers_in(seen_text), key=float)
        if loose_numbers:
            problems.append("numbers on no page read or cited: " + ", ".join(loose_numbers))
        loose_urls = sorted(urls_in(answer) - known_urls)
        if loose_urls:
            problems.append("urls on no page read or cited: " + ", ".join(loose_urls))
        return [
            Grade(
                metric="unsupported_entity",
                value=float(bool(problems)),
                passed=not problems,
                reason="; ".join(problems),
            )
        ]
