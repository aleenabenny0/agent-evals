"""Baseline agents for the docs task (phase 4): what does retrieval actually buy?

ClosedBookAgent answers from model memory with no documentation tools. Every answer is uncited
by construction, so the citation grader records `uncited_answer`; its `correct` rate is the bar
the real agent must beat, and its abstentions show how often the model admits it does not know.

OracleEvidenceAgent is handed the gold evidence pages in the first message and has no search
tool, so retrieval cannot fail. The gap between it and the real agent is the cost of retrieval
mistakes. It reads `case.gold.evidence` on purpose and must never inform a release decision.
"""

from __future__ import annotations

from typing import Any

from agent_evals.agents.docs.agent import DocsAgent, tool_specs
from agent_evals.agents.docs.prompts import CLOSED_BOOK_PROMPT_V1, ORACLE_PROMPT_V1
from agent_evals.config import DocsAgentConfig
from agent_evals.corpus import Corpus
from agent_evals.harness.llm import LLMClient
from agent_evals.types import Case


def _submit_only(strict: bool) -> list[dict[str, Any]]:
    return [spec for spec in tool_specs(strict) if spec["name"] == "submit_answer"]


class ClosedBookAgent(DocsAgent):
    name = "closed-book"
    require_citations = False

    def __init__(self, llm: LLMClient, corpus: Corpus, config: DocsAgentConfig) -> None:
        super().__init__(llm, corpus, config)
        self.system_prompt = CLOSED_BOOK_PROMPT_V1
        self.tools = _submit_only(config.strict_tools)


class OracleEvidenceAgent(DocsAgent):
    name = "oracle-evidence"

    def __init__(self, llm: LLMClient, corpus: Corpus, config: DocsAgentConfig) -> None:
        super().__init__(llm, corpus, config)
        self.system_prompt = ORACLE_PROMPT_V1
        self.tools = _submit_only(config.strict_tools)

    def first_message(self, case: Case) -> str:
        if case.gold is None:
            raise ValueError(f"{case.id}: the oracle baseline needs gold evidence, and has none")
        parts: list[str] = []
        for url in case.gold.evidence_urls:
            text = self.corpus.read(url, 0, self.config.page_chars)
            if text is not None:
                parts.append(text)
        if not parts:
            parts.append("(No documentation pages were provided for this question.)")
        return "\n\n".join(parts) + f"\n\nQuestion: {case.question}"
