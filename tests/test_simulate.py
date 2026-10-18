from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest

from agent_evals.corpus import Corpus
from agent_evals.simulate import (
    AREA_MAP_PATH,
    MAX_ATTEMPTS,
    OUTSIDE_PER_DAY,
    PERSONAS,
    QUOTA,
    SYMPTOM_PER_PERSONA,
    Slot,
    TrafficQuestion,
    day_summary,
    factual_sentences,
    generate_day,
    generation_request,
    headings,
    jaccard,
    load_area_map,
    load_verdicts,
    mentions,
    outside_services,
    page_areas,
    pilot_sample,
    pilot_summary,
    plan_day,
    protected_pages,
    question_sha,
    title_overlap,
)
from agent_evals.types import Area, Gold
from conftest import BUCKETS_URL, LAMBDA_URL, ScriptedLLM, make_case, make_page, text, tool_use

SERVICES = [f"Service{n:02d}" for n in range(12)]
MEMORY = "Functions can use between 128 MB and 10,240 MB of memory."
PROSE_URL = "https://docs.example.test/s3/objects.html"


def ask(q: str) -> list[dict[str, object]]:
    return [tool_use("submit_question", {"question": q})]


def test_plan_day_has_exact_quotas_distinct_outside_services_and_no_excluded_page(
    corpus: Corpus,
) -> None:
    slots = plan_day(1, corpus, excluded_urls={BUCKETS_URL}, outside=SERVICES)
    assert len(slots) == 20 * len(PERSONAS)
    by_cell = Counter((s.persona, s.intent) for s in slots)
    for persona in PERSONAS:
        for intent, n in QUOTA.items():
            assert by_cell[(persona, intent)] == n
        assert sum(1 for s in slots if s.persona == persona and s.symptom) == SYMPTOM_PER_PERSONA
    assert {s.source_url for s in slots if s.intent != "outside_snapshot"} == {LAMBDA_URL}
    outside = [s for s in slots if s.intent == "outside_snapshot"]
    assert len(outside) == len({s.service for s in outside}) == OUTSIDE_PER_DAY
    assert all(s.source_url is None and s.service in SERVICES for s in outside)
    # The page's only prose fact; its table row is never a premise's key.
    assert {s.sentence for s in slots if s.intent == "false_premise"} == {MEMORY}
    assert plan_day(1, corpus, {BUCKETS_URL}, SERVICES) == slots  # a day can be rebuilt exactly


def test_plan_day_refuses_without_pages_or_ten_distinct_services(corpus: Corpus) -> None:
    with pytest.raises(ValueError, match="source pages"):
        plan_day(1, corpus, excluded_urls={BUCKETS_URL, LAMBDA_URL}, outside=SERVICES)
    with pytest.raises(ValueError, match="distinct"):
        plan_day(1, corpus, excluded_urls=set(), outside=SERVICES[:9] + SERVICES[:3])


def test_protected_pages_cover_test_evidence_and_support_sentences_elsewhere(
    corpus: Corpus,
) -> None:
    case = make_case(gold=Gold(answer="up to 10,240 MB", evidence=[BUCKETS_URL], support=[MEMORY]))
    assert protected_pages(corpus, [case]) == {
        "test_evidence": {BUCKETS_URL},
        "test_support_sentence": {LAMBDA_URL},
    }


def test_outside_services_drop_names_on_a_page_or_in_a_test_question(corpus: Corpus) -> None:
    kept, dropped = outside_services(
        ["Lambda", "Braket", "GameLift", "Braket", "Ground Station"],
        corpus,
        ["How many fleets can one GameLift account run?"],
    )
    assert kept == ["Braket", "Ground Station"]
    assert dropped == {
        "Lambda": "named on a snapshot page",
        "GameLift": "named by a test question",
    }


def test_page_areas_prefer_a_page_entry_and_refuse_a_gap() -> None:
    area_map = {"services": {"lambda": "compute", "s3": "storage"}, "pages": {LAMBDA_URL: "ai_ml"}}
    assert page_areas(area_map, [LAMBDA_URL, BUCKETS_URL]) == {
        LAMBDA_URL: "ai_ml",
        BUCKETS_URL: "storage",
    }
    with pytest.raises(ValueError, match="no area"):
        page_areas({"services": {"lambda": "compute"}}, [BUCKETS_URL])


def test_committed_area_map_uses_only_case_areas(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    area_map = load_area_map(root / AREA_MAP_PATH)
    assert area_map["services"] and area_map["pages"]
    assert {a for m in area_map.values() for a in m.values()} <= set(get_args(Area))
    bad = tmp_path / "map.json"
    bad.write_text(json.dumps({"services": {"lambda": "serverless"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown areas"):
        load_area_map(bad)


def test_factual_sentences_keep_prose_facts_and_skip_code_lists_tables_and_pointers() -> None:
    page_text = (
        "# Objects\n\n"
        "Each bucket can hold an unlimited\nnumber of objects by default.\n\n"
        "You can store up to 5 TB in one object, e.g. a video, as described in "
        "[Objects](./o.html).\n\n"
        "```\n# a comment, not a heading\naws s3api put-object --bucket b --body 5 MB.\n```\n\n"
        "* Objects can be up to 5 TB in size and must have a key.\n\n"
        "| Limit | Value |\n|---|---|\n| Object size | 5 TB maximum allowed per object here. |\n\n"
        "Uploads include the following steps for objects up to 5 GB:\n\n"
        "For more information, see the maximum object size of 5 TB in Quotas.\n\n"
        "Use the `x-amz-meta` header to add up to 2 KB of metadata to an object.\n"
    )
    corpus = Corpus([make_page(PROSE_URL, "Objects", page_text)])
    assert factual_sentences(corpus, PROSE_URL) == [
        "Each bucket can hold an unlimited number of objects by default.",  # hard wrap joined
        "You can store up to 5 TB in one object, e.g. a video, as described in Objects.",
    ]
    assert headings(corpus.pages[PROSE_URL]) == ["Objects"]  # not the comment in the fence


def test_requests_carry_persona_task_hint_and_a_strict_tool(corpus: Corpus) -> None:
    page = corpus.pages[LAMBDA_URL]
    premise = Slot(1, 0, "on-call", "false_premise", LAMBDA_URL, None, MEMORY, False)
    req = generation_request(premise, page, "m")
    assert "on-call engineer" in req["system"]
    assert MEMORY in req["messages"][0]["content"]
    assert req["tools"][0]["name"] == "submit_question" and req["tools"][0]["strict"] is True
    assert "tool_choice" not in req  # claude-sonnet-5-5 rejects a forced tool call
    outside = Slot(1, 1, "cost-manager", "outside_snapshot", None, "Braket", None, False)
    content = generation_request(outside, None, "m", hint="Ask another.")["messages"][0]["content"]
    assert "name Braket in it" in content and content.endswith("\nAsk another.")
    symptom = Slot(1, 2, "new-developer", "lookup", LAMBDA_URL, None, None, True)
    symptom_task = generation_request(symptom, page, "m")["messages"][0]["content"]
    assert "problem you ran into" in symptom_task


def test_a_near_duplicate_is_retried_under_a_new_salt_with_the_clash_quoted(
    corpus: Corpus,
) -> None:
    slots = [
        Slot(3, 0, "new-developer", "lookup", LAMBDA_URL, None, None, False),
        Slot(3, 1, "security-reviewer", "lookup", LAMBDA_URL, None, None, False),
        Slot(3, 2, "cost-manager", "outside_snapshot", None, "Braket", None, False),
    ]
    first = "What is the maximum timeout for a Lambda function?"
    llm = ScriptedLLM(
        [
            ask(first),
            ask(first),  # a duplicate: retried
            ask("Who can change the memory setting of a function?"),
            ask("How much does a Braket task cost per shot?"),
        ]
    )
    questions, redo = generate_day(llm, slots, corpus, {LAMBDA_URL: "compute"}, "m")
    assert llm.salts == [
        "online-day03-slot000-try0",
        "online-day03-slot001-try0",
        "online-day03-slot001-try1",
        "online-day03-slot002-try0",
    ]
    assert first in llm.requests[2]["messages"][0]["content"]  # the retry quotes the clash
    assert [q.attempt for q in questions] == [0, 1, 0]
    assert redo == {"failed": 0, "unnamed_service": 0, "duplicate": 1, "redrawn_page": 0}
    assert questions[0].area == "compute" and questions[2].area is None
    assert questions[2].type == "unanswerable" and questions[2].service == "Braket"
    assert questions[1].id == "online-d03-001"
    summary = day_summary(questions)
    assert summary["questions"] == 3 and summary["regenerated"] == 1
    assert summary["by_area"] == {"compute": 2} and summary["outside_services"] == ["Braket"]


def test_failed_replies_and_unnamed_services_are_retried_not_fatal(corpus: Corpus) -> None:
    slot = [Slot(1, 0, "new-developer", "outside_snapshot", None, "Braket", None, False)]
    llm = ScriptedLLM(
        [
            [text("Here is a question")],  # no tool call
            ask("How do quantum tasks get billed?"),  # leaves out the service
            ask("How is an Amazon Braket quantum task billed?"),
        ]
    )
    questions, redo = generate_day(llm, slot, corpus, {}, "m")
    assert questions[0].question == "How is an Amazon Braket quantum task billed?"
    assert questions[0].attempt == 2
    assert redo["failed"] == 1 and redo["unnamed_service"] == 1
    assert 'Use the name "Braket"' in llm.requests[2]["messages"][0]["content"]


def test_a_slot_that_never_yields_a_question_fails_loudly(corpus: Corpus) -> None:
    slot = [Slot(1, 0, "new-developer", "outside_snapshot", None, "Braket", None, False)]
    llm = ScriptedLLM([ask("How is Braket billed?")] * MAX_ATTEMPTS, stop_reason="max_tokens")
    with pytest.raises(ValueError, match="no usable question"):
        generate_day(llm, slot, corpus, {}, "m")
    assert len(llm.salts) == MAX_ATTEMPTS  # an outside slot has no page to redraw


def test_a_slot_stuck_on_duplicates_moves_to_another_page(corpus: Corpus) -> None:
    slots = [
        Slot(4, 0, "on-call", "lookup", LAMBDA_URL, None, None, False),
        Slot(4, 1, "on-call", "lookup", LAMBDA_URL, None, None, False),
    ]
    same = "lambda timeout max??"
    llm = ScriptedLLM([ask(same)] * (1 + MAX_ATTEMPTS) + [ask("why is my bucket name rejected")])
    questions, redo = generate_day(llm, slots, corpus, {}, "m", pool=[BUCKETS_URL, LAMBDA_URL])
    moved = questions[1]
    assert moved.source_url == BUCKETS_URL and moved.redraw == 1 and moved.attempt == 0
    assert moved.persona == "on-call" and moved.intent == "lookup"
    assert llm.salts[-1] == "online-day04-slot001-redo1-try0"
    assert redo["duplicate"] == MAX_ATTEMPTS and redo["redrawn_page"] == 1


def _question(i: int, persona: str) -> TrafficQuestion:
    return TrafficQuestion(
        id=f"online-d00-{i:03d}",
        day=0,
        persona=persona,
        intent="lookup",
        type="lookup",
        question=f"question {i}",
    )


def test_pilot_sample_resume_and_summary(tmp_path: Path) -> None:
    questions = [_question(10 * n + k, p) for n, p in enumerate(PERSONAS) for k in range(3)]
    picked = pilot_sample(questions, day=0, sample=30)  # 6 asked per persona; only 3 exist
    assert len(picked) == 15 and pilot_sample(questions, 0, 30) == picked
    with pytest.raises(ValueError):
        pilot_sample(questions, 0, len(PERSONAS) - 1)
    good = {
        "id": picked[0].id,
        "question_sha": question_sha(picked[0]),
        "intent": "lookup",
        "realistic": True,
        "answerable": "y",
    }
    # A verdict on a question since regenerated under the same id.
    stale = good | {"id": picked[1].id, "question_sha": "0" * 16, "realistic": False}
    path = tmp_path / "pilot.jsonl"
    path.write_text(f'{json.dumps(good)}\n{json.dumps(stale)}\n{{"id": "torn', encoding="utf-8")
    verdicts = load_verdicts(path)
    assert set(verdicts) == {picked[0].id, picked[1].id}  # the torn last line is skipped
    summary = pilot_summary(picked, verdicts)
    assert summary["reviewed"] == 1 and summary["realistic"] == 1  # the stale one is ignored
    assert summary["meets_two_thirds"] is True
    assert summary["in_snapshot_answerable"] == {"yes": 1, "no": 0, "unsure": 0, "of": 1}


def test_similarity_helpers(corpus: Corpus) -> None:
    assert jaccard("Lambda timeout limit", "lambda TIMEOUT limit!") == 1.0
    assert jaccard("a b", "c d") == 0.0
    page = corpus.pages[LAMBDA_URL]
    assert title_overlap("lambda quotas please", page) == pytest.approx(2 / 3)
    assert title_overlap("What is the Lambda timeout?", page) == pytest.approx(1 / 2)  # stopwords
    assert mentions("Is Amazon Braket's simulator free?", "braket")
    assert not mentions("Brakets are not a service", "Braket")
