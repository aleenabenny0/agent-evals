from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest

from agent_evals.corpus import Corpus
from agent_evals.simulate import (
    AREA_MAP_PATH,
    CASE_TYPE,
    MAX_ATTEMPTS,
    OUTSIDE_PER_DAY,
    PERSONAS,
    PILOT_PER_PERSONA,
    QUOTA,
    SYMPTOM_PER_PERSONA,
    Slot,
    TrafficQuestion,
    append_verdict,
    day_summary,
    factual_sentences,
    generate_day,
    generation_request,
    headings,
    jaccard,
    key_sections,
    load_area_map,
    load_verdicts,
    longest_shared_run,
    mentions,
    outside_services,
    page_areas,
    pilot_sample,
    pilot_summary,
    plan_day,
    protected_pages,
    question_sha,
    source_passages,
    title_overlap,
)
from agent_evals.types import Area, Gold
from conftest import BUCKETS_URL, LAMBDA_URL, ScriptedLLM, make_case, make_page, text, tool_use

SERVICES = [f"Service{n:02d}" for n in range(12)]
MEMORY = "Functions can use between 128 MB and 10,240 MB of memory."
PROSE_URL = "https://docs.example.test/s3/objects.html"
OBJECTS_URL = "https://docs.example.test/s3/object-size.html"
MEMORY_URL = "https://docs.example.test/lambda/memory.html"
ODD_URL = "https://docs.example.test/misc/odd.html"
SIZE_SECTION = (
    "An object can be up to 5 TB in size. To upload an object larger than 5 GB, you must use "
    "multipart upload, which splits the object into parts of at least 5 MB each and uploads "
    "them in parallel. You can upload up to 10,000 parts for each object, and a failed part "
    "can be uploaded again on its own, without restarting the parts that already succeeded."
)
CLASSES_SECTION = (
    "S3 Standard stores objects in at least three Availability Zones by default. S3 One "
    "Zone-IA stores objects in a single Availability Zone, so it costs less, but the data is "
    "lost if that zone is destroyed. Use One Zone-IA only for data that you can recreate, such "
    "as thumbnails, or for copies of data that is already stored safely in another AWS Region."
)
MEMORY_SECTION = (
    "Functions can use between 128 MB and 10,240 MB of memory. Lambda allocates CPU power in "
    "proportion to the memory you configure, so a function with more memory also gets more "
    "CPU. You set memory in 1 MB increments, and a new function gets 128 MB by default unless "
    "you choose another value when you create it or update it later."
)
MEMORY_SEC = f"Memory settings\n\n{MEMORY_SECTION}"


@pytest.fixture
def pages() -> Corpus:
    """Two pages with sections long enough to write a lookup from (the shared fixture's pages are
    fragments), plus a callout and a list of links that must never be used."""
    objects = (
        "# Amazon S3 objects\n\n"
        f"## Object size\n\n{SIZE_SECTION}\n\n"
        f"## Storage classes\n\n{CLASSES_SECTION}\n\n"
        "## Note\n\nObject keys are case sensitive, so photo.jpg and Photo.jpg are two objects.\n\n"
        "## Topics\n\n* [Upload](./u.html)\n* [Copy](./c.html)\n* [Delete](./d.html)\n"
    )
    memory = f"# Lambda memory\n\n## Memory settings\n\n{MEMORY_SECTION}\n"
    return Corpus(
        [
            make_page(OBJECTS_URL, "Amazon S3 objects", objects),
            make_page(MEMORY_URL, "Lambda memory", memory),
        ]
    )


def ask(q: str) -> list[dict[str, object]]:
    return [tool_use("submit_question", {"question": q})]


def checked(verdict: str, missing: str = "") -> list[dict[str, object]]:
    return [tool_use("submit_check", {"verdict": verdict, "missing": missing})]


def test_key_sections_keep_substance_and_skip_callouts_links_code_and_fragments(
    pages: Corpus,
) -> None:
    headings_used = [c.heading for c in key_sections(pages, OBJECTS_URL)]
    assert headings_used == ["Object size", "Storage classes"]  # not "Note", not "Topics"
    code = "```\n" + "aws s3 cp local.txt s3://bucket/key.txt\n" * 20 + "```"
    links = " ".join(f"[link {n}](./{n}.html)" for n in range(30))
    odd = (
        f"# Odd\n\n## Code\n\n{code}\n\n## Links\n\n{links}\n\n"
        "## Short\n\nToo short to rest a question on.\n"
    )
    assert key_sections(Corpus([make_page(ODD_URL, "Odd", odd)]), ODD_URL) == []


def test_plan_day_has_exact_quotas_distinct_outside_services_and_no_excluded_page(
    pages: Corpus,
) -> None:
    slots = plan_day(1, pages, excluded_urls={OBJECTS_URL}, outside=SERVICES)
    assert len(slots) == 20 * len(PERSONAS)
    by_cell = Counter((s.persona, s.intent) for s in slots)
    for persona in PERSONAS:
        for intent, n in QUOTA.items():
            assert by_cell[(persona, intent)] == n
        assert sum(1 for s in slots if s.persona == persona and s.symptom) == SYMPTOM_PER_PERSONA
    assert {s.source_url for s in slots if s.intent != "outside_snapshot"} == {MEMORY_URL}
    outside = [s for s in slots if s.intent == "outside_snapshot"]
    assert len(outside) == len({s.service for s in outside}) == OUTSIDE_PER_DAY
    assert all(s.source_url is None and s.service in SERVICES for s in outside)
    grounded = [s for s in slots if s.intent in ("lookup", "comparison")]
    assert grounded and all(s.section == MEMORY_SEC for s in grounded)  # its only real section
    premises = [s for s in slots if s.intent == "false_premise"]
    assert premises and all(s.sentence and s.section is None for s in premises)
    assert all(s.sentence and s.sentence in MEMORY_SECTION for s in premises)
    assert plan_day(1, pages, {OBJECTS_URL}, SERVICES) == slots  # a day can be rebuilt exactly


def test_a_second_half_has_its_own_seed_and_ids_past_the_first(pages: Corpus) -> None:
    first = plan_day(7, pages, set(), SERVICES)
    second = plan_day(7, pages, set(), SERVICES, half=2)
    assert first == plan_day(7, pages, set(), SERVICES, half=1)  # a 100-question day is half 1
    assert [s.index for s in second] == list(range(100, 200))  # ids and cache salts cannot clash
    assert Counter((s.persona, s.intent) for s in second) == Counter(
        (s.persona, s.intent) for s in first
    )
    outside = [s.service for s in second if s.intent == "outside_snapshot"]
    assert len(set(outside)) == OUTSIDE_PER_DAY  # one service per family within the half
    assert outside != [s.service for s in first if s.intent == "outside_snapshot"]  # own seed
    with pytest.raises(ValueError, match="half counts from 1"):
        plan_day(7, pages, set(), SERVICES, half=0)


def test_a_day_names_each_service_family_once(pages: Corpus) -> None:
    services = ["Snowball", "Snow Family", *SERVICES[:10]]  # 12 names, 11 families
    for day in range(12):
        named = {s.service for s in plan_day(day, pages, set(), services) if s.service}
        assert len(named) == OUTSIDE_PER_DAY and not {"Snowball", "Snow Family"} <= named
    with pytest.raises(ValueError, match="distinct"):  # 10 names but 9 families
        plan_day(1, pages, set(), ["Snowball", "Snow Family", *SERVICES[:8]])


def test_retries_carry_every_rejected_draft_and_a_failed_check_is_counted_apart(
    pages: Corpus,
) -> None:
    slots = [
        Slot(6, 0, "cost-manager", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
        Slot(6, 1, "cost-manager", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
    ]
    taken = "What is the most memory a Lambda function can have?"
    vague = "How much memory can a function use, and what does each MB cost?"
    llm = ScriptedLLM(
        [
            ask(taken),
            checked("full"),
            ask(taken),  # slot 1, try 0: a duplicate
            ask(vague),  # try 1
            checked("partial", "the price per MB"),
            ask("Does more memory also give a function more CPU?"),  # try 2
            [text("no verdict")],  # the checker fails
            ask("Does more memory also give a function more CPU?"),  # try 3
            checked("full"),
        ]
    )
    questions, redo = generate_day(llm, slots, pages, {}, "m")
    assert questions[1].attempt == 3
    assert (redo["duplicate"], redo["unanswerable"], redo["check_failed"]) == (1, 1, 1)
    assert redo["failed"] == 0  # the generator never failed; the checker did
    last = llm.requests[7]["messages"][0]["content"]  # slot 1, try 3
    assert taken in last and vague in last and "the price per MB" in last  # both rejections


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


def test_requests_carry_persona_task_hint_and_a_strict_tool(pages: Corpus) -> None:
    page = pages.pages[MEMORY_URL]
    premise = Slot(1, 0, "on-call", "false_premise", MEMORY_URL, None, MEMORY, False)
    req = generation_request(premise, page, "m")
    assert "on-call engineer" in req["system"] and "have not read" in req["system"]
    premise_task = req["messages"][0]["content"]
    assert (
        MEMORY in premise_task and "Ask nothing that the sentence does not settle" in premise_task
    )
    assert req["tools"][0]["name"] == "submit_question" and req["tools"][0]["strict"] is True
    assert "tool_choice" not in req  # claude-sonnet-5-5 rejects a forced tool call
    outside = Slot(1, 1, "cost-manager", "outside_snapshot", None, "Braket", None, False)
    content = generation_request(outside, None, "m", hint="Ask another.")["messages"][0]["content"]
    assert "name Braket in it" in content and content.endswith("\nAsk another.")
    symptom = Slot(1, 2, "new-developer", "lookup", MEMORY_URL, None, None, True, MEMORY_SEC)
    symptom_task = generation_request(symptom, page, "m")["messages"][0]["content"]
    assert MEMORY_SECTION in symptom_task and "answers completely" in symptom_task
    assert "problem you ran into" in symptom_task


def test_a_near_duplicate_is_retried_under_a_new_salt_with_the_clash_quoted(
    pages: Corpus,
) -> None:
    slots = [
        Slot(3, 0, "new-developer", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
        Slot(3, 1, "security-reviewer", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
        Slot(3, 2, "cost-manager", "outside_snapshot", None, "Braket", None, False),
    ]
    first = "How much memory can a Lambda function have at most?"
    llm = ScriptedLLM(
        [
            ask(first),
            checked("full"),
            ask(first),  # a duplicate: retried before any check
            ask("Does giving a function more memory also give it more CPU?"),
            checked("full"),
            ask("How much does a Braket task cost per shot?"),  # outside: nothing to check
        ]
    )
    questions, redo = generate_day(llm, slots, pages, {MEMORY_URL: "compute"}, "m")
    assert llm.salts == [
        "online-day03-slot000-try0",
        "online-day03-slot000-try0-check",
        "online-day03-slot001-try0",
        "online-day03-slot001-try1",
        "online-day03-slot001-try1-check",
        "online-day03-slot002-try0",
    ]
    assert first in llm.requests[3]["messages"][0]["content"]  # the retry quotes the clash
    assert [q.attempt for q in questions] == [0, 1, 0]
    assert redo == {
        "failed": 0,
        "check_failed": 0,
        "unnamed_service": 0,
        "duplicate": 1,
        "unanswerable": 0,
        "redrawn_page": 0,
    }
    assert questions[0].area == "compute" and questions[2].area is None
    assert questions[0].section == MEMORY_SEC and questions[2].section is None
    assert questions[2].type == "unanswerable" and questions[2].service == "Braket"
    assert questions[1].id == "online-d03-001"
    summary = day_summary(questions)
    assert summary["questions"] == 3 and summary["regenerated"] == 1
    assert summary["by_area"] == {"compute": 2} and summary["outside_services"] == ["Braket"]


def test_a_question_its_section_does_not_fully_answer_is_retried_with_the_gap_named(
    pages: Corpus,
) -> None:
    slot = [Slot(5, 0, "cost-manager", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC)]
    vague = "How much memory can a function use, and what does each MB cost?"
    llm = ScriptedLLM(
        [
            ask(vague),
            checked("partial", "the price per MB"),
            ask("What is the most memory I can give a Lambda function?"),
            checked("full"),
        ]
    )
    questions, redo = generate_day(llm, slot, pages, {}, "m")
    assert questions[0].attempt == 1 and redo["unanswerable"] == 1
    assert llm.salts == [
        "online-day05-slot000-try0",
        "online-day05-slot000-try0-check",
        "online-day05-slot000-try1",
        "online-day05-slot000-try1-check",
    ]
    check = llm.requests[1]
    assert check["system"].startswith("You check whether a passage")
    assert (
        MEMORY_SECTION in check["messages"][0]["content"]
        and vague in check["messages"][0]["content"]
    )
    retry = llm.requests[2]["messages"][0]["content"]
    assert vague in retry and "the price per MB" in retry


def test_longest_shared_run_counts_words_copied_in_a_row() -> None:
    assert longest_shared_run("can I go above 10,240 MB of memory?", MEMORY_SECTION) == 5
    assert longest_shared_run("totally unrelated words", MEMORY_SECTION) == 0


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
    assert 'does not use the name "Braket"' in llm.requests[2]["messages"][0]["content"]


def test_a_slot_that_never_yields_a_question_fails_loudly(corpus: Corpus) -> None:
    slot = [Slot(1, 0, "new-developer", "outside_snapshot", None, "Braket", None, False)]
    llm = ScriptedLLM([ask("How is Braket billed?")] * MAX_ATTEMPTS, stop_reason="max_tokens")
    with pytest.raises(ValueError, match="no usable question"):
        generate_day(llm, slot, corpus, {}, "m")
    assert len(llm.salts) == MAX_ATTEMPTS  # an outside slot has no page to redraw


def test_a_slot_stuck_on_duplicates_moves_to_another_page(pages: Corpus) -> None:
    slots = [
        Slot(4, 0, "on-call", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
        Slot(4, 1, "on-call", "lookup", MEMORY_URL, None, None, False, MEMORY_SEC),
    ]
    same = "lambda memory max??"
    script = [ask(same), checked("full")] + [ask(same)] * MAX_ATTEMPTS
    llm = ScriptedLLM([*script, ask("biggest s3 object i can upload"), checked("full")])
    questions, redo = generate_day(llm, slots, pages, {}, "m", pool=[OBJECTS_URL, MEMORY_URL])
    moved = questions[1]
    assert moved.source_url == OBJECTS_URL and moved.redraw == 1 and moved.attempt == 0
    assert moved.section is not None
    assert moved.section.startswith(("Object size", "Storage classes"))
    assert moved.persona == "on-call" and moved.intent == "lookup"
    assert llm.salts[-2:] == [
        "online-day04-slot001-redo1-try0",
        "online-day04-slot001-redo1-try0-check",
    ]
    assert redo["duplicate"] == MAX_ATTEMPTS and redo["redrawn_page"] == 1


def _day() -> list[TrafficQuestion]:
    """A day of minimal questions, QUOTA per persona."""
    out: list[TrafficQuestion] = []
    for persona in PERSONAS:
        for intent, n in QUOTA.items():
            for k in range(n):
                out.append(
                    TrafficQuestion(
                        id=f"online-d00-{len(out):03d}",
                        day=0,
                        persona=persona,
                        intent=intent,
                        type=CASE_TYPE[intent],
                        question=f"{persona} asks {intent} question {k}?",
                    )
                )
    return out


def _verdict(q: TrafficQuestion, realistic: bool, answerable: str = "y") -> dict[str, object]:
    return {
        "id": q.id,
        "question_sha": question_sha(q),
        "intent": q.intent,
        "realistic": realistic,
        "answerable": answerable,
    }


def test_pilot_sample_is_stratified_by_persona_and_intent_and_reproducible() -> None:
    picked = pilot_sample(_day(), day=0)
    assert len(picked) == 30 and pilot_sample(_day(), 0) == picked
    cells = Counter((q.persona, q.intent) for q in picked)
    assert all(cells[(p, i)] == n for p in PERSONAS for i, n in PILOT_PER_PERSONA.items())


def test_pilot_bar_weighs_intents_by_traffic_and_waits_for_the_last_answer() -> None:
    picked = pilot_sample(_day(), day=0)
    # Every lookup realistic and nothing else: 15 of 30 unweighted, 70% of traffic weighted.
    summary = pilot_summary(picked, {q.id: _verdict(q, q.intent == "lookup") for q in picked})
    assert summary["complete"] and summary["realistic_share"] == 0.5
    assert summary["realistic_share_weighted"] == 0.7 and summary["meets_two_thirds"] is True
    assert summary["by_intent"]["outside_snapshot"]["answerable_expected"] == "n"
    flipped = {q.id: _verdict(q, q.intent != "lookup") for q in picked}  # 30% weighted
    assert pilot_summary(picked, flipped)["meets_two_thirds"] is False
    # One answer short of the end, the bar is undecided, however good the rate so far.
    partial = pilot_summary(picked, {q.id: _verdict(q, True) for q in picked[:-1]})
    assert not partial["complete"] and partial["meets_two_thirds"] is None


def test_stale_and_torn_verdicts_are_skipped_and_the_next_answer_survives(tmp_path: Path) -> None:
    picked = pilot_sample(_day(), day=0)
    good = _verdict(picked[0], True)
    stale = _verdict(picked[1], False) | {"question_sha": "0" * 16}  # question since regenerated
    path = tmp_path / "pilot.jsonl"
    lines = [json.dumps(good), json.dumps(stale), '{"id": "torn']  # the last one has no end
    path.write_text("\n".join(lines), encoding="utf-8")
    append_verdict(path, _verdict(picked[2], True))  # written after the torn last line
    verdicts = load_verdicts(path)
    assert set(verdicts) == {picked[0].id, picked[1].id, picked[2].id}
    summary = pilot_summary(picked, verdicts)
    assert summary["reviewed"] == 2 and not summary["complete"]  # the stale one does not count


def test_source_passages_rank_the_page_by_the_question(corpus: Corpus) -> None:
    q = TrafficQuestion(
        id="online-d00-000",
        day=0,
        persona="on-call",
        intent="lookup",
        type="lookup",
        question="what is the memory limit",
        source_url=LAMBDA_URL,
    )
    passages = source_passages(corpus, q)
    assert passages and "10,240 MB" in passages[0][1]


def test_similarity_helpers(corpus: Corpus) -> None:
    assert jaccard("Lambda timeout limit", "lambda TIMEOUT limit!") == 1.0
    assert jaccard("a b", "c d") == 0.0
    page = corpus.pages[LAMBDA_URL]
    assert title_overlap("lambda quotas please", page) == pytest.approx(2 / 3)
    assert title_overlap("What is the Lambda timeout?", page) == pytest.approx(1 / 2)  # stopwords
    assert mentions("Is Amazon Braket's simulator free?", "braket")
    assert not mentions("Brakets are not a service", "Braket")
