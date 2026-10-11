from __future__ import annotations

from agent_evals.agents.docs.agent import DocsAgent
from agent_evals.agents.docs.baselines import ClosedBookAgent, OracleEvidenceAgent
from agent_evals.config import DocsAgentConfig
from agent_evals.corpus import Corpus
from agent_evals.types import Case, RunContext
from conftest import ScriptedLLM, tool_use

CTX = RunContext(run_id="r", case_id="t-1", attempt=1)
UNCITED = {
    "answer": "Bucket names are 3 to 63 characters.",
    "citations": [],
    "abstained": False,
    "confidence": 0.7,
}


def test_closed_book_has_only_submit_and_accepts_uncited_answers(
    corpus: Corpus, case: Case
) -> None:
    llm = ScriptedLLM([[tool_use("submit_answer", UNCITED)]])
    agent = ClosedBookAgent(llm, corpus, DocsAgentConfig())
    assert [t["name"] for t in agent.tools] == ["submit_answer"]
    result = agent.run(case, CTX)
    assert result.output == UNCITED and result.error is None
    assert llm.requests[0]["messages"][0]["content"] == case.question


def test_oracle_gets_the_evidence_pages_and_still_needs_citations(
    corpus: Corpus, case: Case
) -> None:
    llm = ScriptedLLM([[tool_use("submit_answer", UNCITED)]])
    agent = OracleEvidenceAgent(llm, corpus, DocsAgentConfig(max_submission_rejections=1))
    result = agent.run(case, CTX)
    first = llm.requests[0]["messages"][0]["content"]
    assert "Bucket names must be between" in first and first.endswith(f"Question: {case.question}")
    assert result.error is not None and result.error.startswith("malformed_submission")


def test_baseline_versions_differ_from_the_docs_agent(corpus: Corpus) -> None:
    cfg = DocsAgentConfig()
    versions = {
        cls(ScriptedLLM([]), corpus, cfg).version
        for cls in (DocsAgent, ClosedBookAgent, OracleEvidenceAgent)
    }
    assert len(versions) == 3
