from __future__ import annotations

import pytest

from agent_evals.agents.docs.agent import NUDGE, DocsAgent, tool_specs
from agent_evals.config import DocsAgentConfig
from agent_evals.corpus import Corpus
from agent_evals.types import Case, RunContext
from conftest import BUCKETS_URL, ScriptedLLM, text, tool_use

CTX = RunContext(run_id="r", case_id="t-1", attempt=1)
GOOD_ANSWER = {
    "answer": "Bucket names must be 3 to 63 characters.",
    "citations": [{"url": BUCKETS_URL, "quote": "between 3 and 63 characters long"}],
    "abstained": False,
    "confidence": 0.9,
}
# The shape seen on docs-smoke-003: citations written as tool-call markup inside `answer`.
LEAKED_ANSWER = {
    "answer": (
        "Bucket names must be 3 to 63 characters.</answer>\n"
        f'<parameter name="citations">[{{"url": "{BUCKETS_URL}", "quote": "3 and 63"}}]'
    ),
    "citations": [],
    "abstained": False,
    "confidence": 0.9,
}


def make_agent(llm: ScriptedLLM, corpus: Corpus, **cfg: object) -> DocsAgent:
    return DocsAgent(llm, corpus, DocsAgentConfig.model_validate(cfg))


def test_search_read_submit(corpus: Corpus, case: Case) -> None:
    llm = ScriptedLLM(
        [
            [text("Searching."), tool_use("search_docs", {"query": "bucket name length"}, "t1")],
            [tool_use("read_page", {"url": BUCKETS_URL, "start": 0}, "t2")],
            [tool_use("submit_answer", GOOD_ANSWER, "t3")],
        ]
    )
    result = make_agent(llm, corpus).run(case, CTX)

    assert result.error is None
    assert result.output == GOOD_ANSWER
    assert [s.name for s in result.trajectory if s.kind == "tool"] == [
        "search_docs",
        "read_page",
        "submit_answer",
    ]
    assert result.usage.model_calls == 3
    # The tool result for read_page went back to the model in the next request.
    last_user = llm.requests[2]["messages"][-1]["content"]
    assert last_user[0]["type"] == "tool_result" and "63" in last_user[0]["content"]


def test_unknown_page_is_reported_back_as_tool_error(corpus: Corpus, case: Case) -> None:
    llm = ScriptedLLM(
        [
            [tool_use("read_page", {"url": "https://nope.test", "start": 0})],
            [tool_use("submit_answer", GOOD_ANSWER)],
        ]
    )
    result = make_agent(llm, corpus).run(case, CTX)
    read_step = next(s for s in result.trajectory if s.name == "read_page")
    assert read_step.is_error
    assert result.error is None


def test_invalid_submission_gets_a_chance_to_fix(corpus: Corpus, case: Case) -> None:
    bad = {**GOOD_ANSWER, "confidence": 7}
    llm = ScriptedLLM(
        [
            [tool_use("submit_answer", bad, "a")],
            [tool_use("submit_answer", GOOD_ANSWER, "b")],
        ]
    )
    result = make_agent(llm, corpus).run(case, CTX)
    assert result.output == GOOD_ANSWER
    assert sum(s.is_error for s in result.trajectory) == 1


def test_nudges_once_then_gives_up(corpus: Corpus, case: Case) -> None:
    llm = ScriptedLLM([[text("I think it's 63.")], [text("Still 63.")]])
    result = make_agent(llm, corpus).run(case, CTX)
    assert result.error == "no_submission"
    assert llm.requests[1]["messages"][-1]["content"] == NUDGE


def test_stops_at_max_steps(corpus: Corpus, case: Case) -> None:
    llm = ScriptedLLM([[tool_use("search_docs", {"query": "x"})]] * 3)
    result = make_agent(llm, corpus, max_steps=3).run(case, CTX)
    assert result.error == "max_steps"


def test_version_changes_with_behavior(corpus: Corpus) -> None:
    a = make_agent(ScriptedLLM([]), corpus)
    b = make_agent(ScriptedLLM([]), corpus, max_steps=99)
    assert a.version != b.version
    assert a.version == make_agent(ScriptedLLM([]), corpus).version


def test_strict_flag() -> None:
    assert all(t["strict"] for t in tool_specs(True))
    assert not any("strict" in t for t in tool_specs(False))


def _sent_back(llm: ScriptedLLM, request_index: int) -> dict[str, object]:
    """The tool_result the agent returned for the submission in the previous model turn."""
    block: dict[str, object] = llm.requests[request_index]["messages"][-1]["content"][0]
    return block


def test_uncited_submission_is_rejected_then_accepted(corpus: Corpus, case: Case) -> None:
    uncited = {**GOOD_ANSWER, "citations": []}
    llm = ScriptedLLM(
        [
            [tool_use("submit_answer", uncited, "a")],
            [tool_use("submit_answer", GOOD_ANSWER, "b")],
        ]
    )
    result = make_agent(llm, corpus).run(case, CTX)
    assert result.output == GOOD_ANSWER
    sent_back = _sent_back(llm, 1)
    assert sent_back["is_error"] and "citations is empty" in str(sent_back["content"])


def test_leaked_markup_submission_is_rejected_then_accepted(corpus: Corpus, case: Case) -> None:
    llm = ScriptedLLM(
        [
            [tool_use("submit_answer", LEAKED_ANSWER, "a")],
            [tool_use("submit_answer", GOOD_ANSWER, "b")],
        ]
    )
    result = make_agent(llm, corpus).run(case, CTX)
    assert result.output == GOOD_ANSWER
    assert sum(s.is_error for s in result.trajectory) == 1
    reason = str(_sent_back(llm, 1)["content"])
    # Both problems are reported so the model can fix them in one go.
    assert "</answer>" in reason and "citations is empty" in reason


def test_abstention_without_citations_is_accepted(corpus: Corpus, case: Case) -> None:
    abstention = {
        "answer": "The snapshot has no page about this.",
        "citations": [],
        "abstained": True,
        "confidence": 0.8,
    }
    llm = ScriptedLLM([[tool_use("submit_answer", abstention)]])
    result = make_agent(llm, corpus).run(case, CTX)
    assert result.output == abstention
    assert not any(s.is_error for s in result.trajectory)


def test_version_changes_with_rejection_messages(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = make_agent(ScriptedLLM([]), corpus).version
    monkeypatch.setattr("agent_evals.agents.docs.agent.REJECT_UNCITED_V1", "different text")
    assert make_agent(ScriptedLLM([]), corpus).version != before
