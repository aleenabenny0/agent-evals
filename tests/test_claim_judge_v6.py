"""Judge v6's rules in the claim judge (docs/plans/judge-v6.md, changes 1 to 4): the gap type per
claim, the cited service's own name cleared in code, the grid as levels, the names count, and the
service-name file they read. The first draft's prompts and tools are judge v5's, byte for byte."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_evals.config import JudgeConfig
from agent_evals.graders import claim_judge as cj
from agent_evals.graders.claim_judge import Assessment, ClaimJudgeGrader, FlaggedItem, normalize
from agent_evals.graders.judge_prompts import (
    GAP_TYPES,
    SUPPORT_RUBRICS,
    claims_prompt,
    claims_tool,
    skeptic_prompt,
    skeptic_tool,
)
from agent_evals.graders.service_names import ServiceNames
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.types import AgentResult
from conftest import ScriptedLLM, make_case, tool_use

ROOT = Path(__file__).resolve().parents[1]
BEDROCK_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html"
GENERAL_URL = "https://docs.aws.amazon.com/general/latest/gr/bedrock.html#limits"
QUOTE = "Cross-Region inference routes requests to other Regions when traffic is high."
# The hashes of judge v5's prompts and tools (revision 1, commit 7a0231a), which the first draft
# keeps: with gap_types off, the text and the schemas are the same bytes.
V5_HASHES = {
    "claims_prompt": "0e1078f16bd3e50317d3a42375ecb8e1c0e4d98f6da77622806d7a5a7044bd33",
    "skeptic_prompt": "2a0655fde339116daa986ab44f0b899252b30b5037e0362d2f9781cbbba011fa",
    "claims_tool": "1e141b3b427743c3c06270c1566dc90e0ee4e3ef812b81555f1f78572e0042ef",
    "skeptic_tool": "351589077a13b3fea48a58fd968cfd87442116dcb86266f92c014a9a3bfad001",
}


def names() -> ServiceNames:
    return ServiceNames.from_data(
        {
            "publisher": ["AWS", "Amazon"],
            "segments": {
                "bedrock": [
                    {"name": "Amazon Bedrock", "source": "title"},
                    {"name": "Bedrock", "source": "short"},
                ],
                "sagemaker": [{"name": "Amazon SageMaker AI", "source": "title"}],
            },
            "pages": {
                "/general/latest/gr/bedrock.html": [{"name": "Amazon Bedrock", "source": "title"}]
            },
        }
    )


def config(**overrides: Any) -> JudgeConfig:
    base = {
        "method": "claims",
        "grader_name": "judge-v6",
        "rubric_version": "v5",
        "responsive": False,
        "support_threshold": 69,
        "skeptic_threshold": None,
        "model": {"name": "claude-sonnet-5-5", "max_tokens": 8192},
    }
    return JudgeConfig.model_validate(base | overrides)


def grader(llm: ScriptedLLM | None = None, **overrides: Any) -> ClaimJudgeGrader:
    return ClaimJudgeGrader(llm or ScriptedLLM([]), config(**overrides), service_names=names())


def answered(answer: str, *quotes: str, url: str = BEDROCK_URL) -> AgentResult:
    citations = [{"url": url, "quote": q} for q in quotes or (QUOTE,)]
    return AgentResult(output={"answer": answer, "citations": citations, "abstained": False})


def claim(
    span: str, score: int = 90, gap: str | None = None, term: str = "", **more: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "answer_span": span,
        "role": more.get("role", "core"),
        "support": [
            {"quote": 1, "quote_span": more.get("quote_span", "routes requests to other Regions")}
        ],
        "score": score,
        "why": "w",
    }
    if gap is not None:
        entry["gap"] = gap
        entry["term"] = term
    return entry


def table(*claims: dict[str, Any], not_claims: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [tool_use("submit_claims", {"claims": list(claims), "not_claims": list(not_claims)})]


def skeptic(
    scores: dict[int, int] | None = None, *, no_claim: tuple[int, ...] = ()
) -> list[dict[str, Any]]:
    flagged = [
        {"item": n, "score": s, "not_a_claim": n in no_claim, "why": "w"}
        for n, s in (scores or {}).items()
    ]
    search = {"found": False, "answer_span": "", "role": "core", "why": "", "confidence": 0}
    return [tool_use("submit_skeptic", {"flagged": flagged, "search": search})]


def assessed(llm: ScriptedLLM, answer: str, *quotes: str, **overrides: Any) -> Assessment:
    found = grader(llm, **overrides).assess(
        make_case(question="How does routing work?"), answered(answer, *quotes)
    )
    assert found is not None
    return found


# ---- the first draft keeps judge v5's prompts and tools -------------------------------------


def test_with_gap_types_off_the_prompts_and_tools_are_judge_v5s_bytes() -> None:
    rubric = SUPPORT_RUBRICS["v5"]
    assert sha256_text(claims_prompt(rubric)) == V5_HASHES["claims_prompt"]
    assert sha256_text(skeptic_prompt(rubric)) == V5_HASHES["skeptic_prompt"]
    assert sha256_text(canonical_json(claims_tool(True))) == V5_HASHES["claims_tool"]
    assert sha256_text(canonical_json(skeptic_tool(True))) == V5_HASHES["skeptic_tool"]
    draft = grader()
    assert draft.config.gap_types is False
    assert sha256_text(draft.claims_prompt) == V5_HASHES["claims_prompt"]
    assert (
        "gap"
        not in draft.claims_tool["input_schema"]["properties"]["claims"]["items"]["properties"]
    )


def test_with_gap_types_on_both_prompts_define_the_gaps_and_both_tools_carry_them() -> None:
    rubric = SUPPORT_RUBRICS["v5"]
    on = grader(gap_types=True)
    for prompt in (on.claims_prompt, on.skeptic_prompt):
        assert GAP_TYPES in prompt and "The publisher (AWS, Amazon" in prompt
    assert "- gap and term: as defined above." in on.claims_prompt
    assert "name its gap and, for a term, copy the term too" in on.skeptic_prompt
    claim_schema = on.claims_tool["input_schema"]["properties"]["claims"]["items"]
    assert claim_schema["properties"]["gap"]["enum"] == list(cj.GAPS)
    assert claim_schema["required"][-2:] == ["gap", "term"]
    search = on.skeptic_tool["input_schema"]["properties"]["search"]
    assert search["properties"]["gap"]["enum"] == list(cj.GAPS) and "term" in search["required"]
    # Off is v5's; on is another version; the two prompts' definitions are one text.
    assert claims_prompt(rubric, True) != claims_prompt(rubric) and on.version != grader().version
    assert (
        claims_prompt(rubric, True).count(GAP_TYPES)
        == skeptic_prompt(rubric, True).count(GAP_TYPES)
        == 1
    )


# ---- change 1: the gap rule, the downgrade and the term record -------------------------------


def test_a_term_in_no_quote_and_a_headerless_cell_fail_at_every_t_the_rest_are_scored() -> None:
    answer = "It routes requests to other Regions. Harness invokes agents. The limit is 5."
    llm = ScriptedLLM(
        [
            table(
                claim("routes requests to other Regions", 95, "none"),
                claim("Harness invokes agents", 90, "term", "Harness"),
                claim("The limit is 5", 90, "cell", role="peripheral"),
            ),
            skeptic({1: 90}),
        ]
    )
    found = assessed(llm, answer, gap_types=True)
    by_span = {c.answer_span: c for c in found.claims}
    assert [c.rule_fails for c in found.claims] == [False, True, True]
    assert by_span["Harness invokes agents"].term == "Harness"
    assert by_span["Harness invokes agents"].term_quoted is False
    assert by_span["routes requests to other Regions"].term_quoted is None  # no term
    for t in cj.T_GRID:
        v = found.verdict(t, None)
        assert not v.fully_supported and not v.core_supported  # the term claim is core
        assert v.rule_fails == 2
    assert (
        'the term "Harness" is in no quote, which fails at every t'
        in found.verdict(69, None).reason
    )
    counts = cj.JudgingCounts.of(found, 69, None)
    assert (counts.gaps_none, counts.gaps_term, counts.gaps_cell, counts.rule_fails) == (1, 1, 1, 2)
    assert (counts.terms_quoted, counts.terms_unquoted, counts.downgrades) == (0, 1, 0)


def test_a_term_that_is_the_cited_services_or_publishers_name_is_downgraded_and_scored() -> None:
    answer = "Bedrock routes requests to other Regions. Amazon's docs say so."
    llm = ScriptedLLM(
        [
            table(
                claim("Bedrock routes requests to other Regions", 70, "term", "Bedrock"),
                claim("Amazon's docs say so", 65, "term", "Amazon's", role="peripheral"),
            ),
            skeptic(),
        ]
    )
    found = assessed(llm, answer, gap_types=True)
    assert [c.downgraded for c in found.claims] == [True, True]
    assert not any(c.rule_fails for c in found.claims)
    assert found.verdict(64, None).fully_supported  # scored as v5 would: 70 and 65 pass at 64
    assert found.verdict(69, None).core_supported and not found.verdict(69, None).fully_supported
    assert found.cited_names == ["Amazon Bedrock", "Bedrock", "AWS", "Amazon"]
    assert cj.JudgingCounts.of(found, 69, None).downgrades == 2
    # A feature of the service is not the service's own name.
    other = assessed(
        ScriptedLLM(
            [
                table(
                    claim(
                        "Bedrock Agents routes requests to other Regions",
                        90,
                        "term",
                        "Bedrock Agents",
                    )
                ),
                skeptic(),
            ]
        ),
        "Bedrock Agents routes requests to other Regions.",
        gap_types=True,
    )
    assert not other.claims[0].downgraded and other.claims[0].rule_fails


def test_the_term_record_reads_quotes_and_the_question_but_never_decides() -> None:
    answer = "Requests go to other Regions, where cross-Region inference applies."
    span = "Requests go to other Regions, where cross-Region inference applies"
    llm = ScriptedLLM([table(claim(span, 90, "term", "Cross-Region inference")), skeptic()])
    found = assessed(llm, answer, gap_types=True)
    assert found.claims[0].term_quoted is True  # quote 1 has it, in another case
    assert found.claims[0].rule_fails and not found.verdict(64, None).fully_supported  # still fails
    assert cj.JudgingCounts.of(found, 69, None).terms_quoted == 1
    asked = ScriptedLLM([table(claim("routing works", 90, "term", "routing")), skeptic()])
    found = assessed(asked, "That is how routing works.", gap_types=True)
    assert found.claims[0].term_quoted is True  # the question has it


def test_a_gap_type_is_required_when_asked_for_and_a_term_gap_needs_its_term() -> None:
    without = {k: v for k, v in claim("routes requests to other Regions", 90).items()}
    llm = ScriptedLLM(
        [table(without), table(claim("routes requests to other Regions", 90, "none")), skeptic()]
    )
    found = assessed(llm, "It routes requests to other Regions.", gap_types=True)
    assert found.retries == 1 and found.error is None  # asked again under the retry salt
    assert llm.salts[1] == "judge#claims-retry#t-1"
    empty = ScriptedLLM(
        [
            table(claim("Harness invokes agents", 90, "term", "  ")),
            table(claim("Harness invokes agents", 90, "term", "Harness")),
            skeptic(),
        ]
    )
    found = assessed(empty, "Harness invokes agents.", gap_types=True)
    assert found.retries == 1 and found.claims[0].term == "Harness"
    # Off, a v5 table has no gap, and nothing is asked twice for it.
    found = assessed(
        ScriptedLLM([table(without), skeptic()]), "It routes requests to other Regions."
    )
    assert found.retries == 0 and found.claims[0].gap is None and not found.claims[0].rule_fails


# ---- change 2: the cited service's own name inside words of no claim -------------------------


def hedged(
    not_claim: str, skeptic_score: int, *, cite: str = BEDROCK_URL, items: int = 1
) -> Assessment:
    answer = f"It routes requests to other Regions. {not_claim}"
    llm = ScriptedLLM(
        [
            table(claim("routes requests to other Regions", 95), not_claims=(not_claim,)),
            skeptic({n: skeptic_score for n in range(1, items + 1)}),
        ]
    )
    found = grader(llm).assess(
        make_case(question="How does routing work?"), answered(answer, url=cite)
    )
    assert found is not None
    return found


def test_the_services_own_name_in_a_hedge_is_cleared_by_code_whatever_the_skeptic_scored() -> None:
    hedge = "Note that this applies to Bedrock specifically; I did not check other services."
    found = hedged(hedge, skeptic_score=35)
    [item] = found.flagged
    assert (item.kind, item.token) == ("not_claims", "Bedrock")
    assert found.cleared_names() == [item]
    v = found.verdict(69, None)
    assert v.fully_supported and v.names_cleared == 1 and v.names_cleared_skeptic_low == 1
    assert (
        'cleared by code as the cited service\'s own name: "Bedrock" (skeptic score 35)' in v.reason
    )
    counts = cj.JudgingCounts.of(found, 69, None)
    assert (counts.names_cleared, counts.names_cleared_skeptic_low) == (1, 1)


def test_a_hedge_that_names_a_source_stays_with_the_skeptic() -> None:
    for hedge in (
        "This rests on the Bedrock pricing page alone.",
        "I read this in the Bedrock documentation only.",
        "The Bedrock console shows the quota; I did not check it.",
    ):
        found = hedged(hedge, skeptic_score=35)
        assert found.cleared_names() == []
        assert not found.verdict(69, None).fully_supported  # the skeptic's 35 fails it, as in v5
        assert hedged(hedge, skeptic_score=90).verdict(69, None).fully_supported


def test_a_name_of_another_service_or_a_word_of_a_longer_name_is_not_cleared() -> None:
    found = hedged("Note that this applies to SageMaker specifically.", skeptic_score=35)
    assert found.cleared_names() == [] and not found.verdict(69, None).fully_supported
    # SageMaker is a listed name of a cited service only when its pages are cited.
    cited = hedged(
        "Note that this applies to SageMaker specifically.",
        skeptic_score=35,
        cite="https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html",
    )
    assert (
        cited.cleared_names() == [] and "Amazon SageMaker AI" in cited.cited_names
    )  # "SageMaker" alone is no listed name
    # A whole listed name clears; a word inside a longer name does not clear on its own.
    whole = hedged(
        "Note that this applies to Amazon SageMaker AI specifically.",
        skeptic_score=35,
        cite="https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html",
        items=3,
    )
    assert [i.token for i in whole.cleared_names()] == ["Amazon", "SageMaker", "AI"]
    assert whole.verdict(69, None).fully_supported


def test_the_clearing_reads_the_assessments_cited_names_so_a_v5_record_clears_nothing() -> None:
    item = FlaggedItem(
        number=1,
        kind="not_claims",
        token="Bedrock",
        context="This applies to Bedrock specifically.",
    )
    reply = cj.SkepticReply(
        flagged=[cj.ItemScore(item=1, score=35, not_a_claim=False, why="w")],
        search=cj.Search(found=False, answer_span="", role="core", why="", confidence=0),
    )
    v5_record = Assessment(flagged=[item], skeptic=reply)
    assert v5_record.cleared_names() == [] and not v5_record.verdict(69, None).fully_supported
    read_again = v5_record.model_copy(
        update={"cited_names": ["Amazon Bedrock", "Bedrock", "AWS", "Amazon"]}
    )
    assert read_again.cleared_names() == [item] and read_again.verdict(69, None).fully_supported
    publisher_only = v5_record.model_copy(update={"cited_names": ["AWS", "Amazon"]})
    assert publisher_only.cleared_names() == []


# ---- change 3: the grid as levels -------------------------------------------------------------


def test_the_grid_is_the_levels_65_70_and_75_and_50_and_60() -> None:
    assert (cj.T_GRID, cj.S_GRID, cj.SKEPTIC_GATE) == ((64, 69, 74), (None, 49, 59), 64)
    for t in (50, 65, 70, 75, 85):
        with pytest.raises(ValueError, match="support_threshold is"):
            grader(support_threshold=t)
    for s in (50, 60, 70, 90):
        with pytest.raises(ValueError, match="skeptic_threshold is"):
            grader(skeptic_threshold=s)
    assert JudgeConfig().support_threshold == 69 and JudgeConfig().skeptic_threshold is None


# ---- change 4: names inside claims, counted only -------------------------------------------------


def test_names_inside_a_claim_that_no_quote_states_are_counted_and_never_decide() -> None:
    answer = (
        "Bedrock routes requests to other Regions. You can use Harness, Strands and Agents with "
        "the CLI, and AWS says so."
    )
    llm = ScriptedLLM(
        [
            table(
                claim("Bedrock routes requests to other Regions", 95),
                claim(
                    "You can use Harness, Strands and Agents with the CLI", 95, role="peripheral"
                ),
                claim("AWS says so", 95, role="peripheral"),
            ),
            skeptic(),
        ]
    )
    found = assessed(llm, answer)
    # Bedrock is the cited service's name, Regions is in the quote, CLI is generic, AWS is the
    # publisher; Harness, Strands and Agents are names no quote states.
    assert [c.names_unquoted for c in found.claims] == [0, 3, 0]
    assert found.verdict(69, None).fully_supported  # counted, not decided
    counts = cj.JudgingCounts.of(found, 69, None)
    assert (counts.names_unquoted, counts.claims_with_names_unquoted) == (3, 1)


# ---- the version, the run note and the service-name file --------------------------------------


def test_the_version_moves_with_gap_types_the_word_lists_and_the_service_name_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = grader().version
    assert grader(gap_types=True).version != base
    other_names = ServiceNames.from_data({"publisher": ["AWS"], "segments": {}, "pages": {}})
    assert ClaimJudgeGrader(ScriptedLLM([]), config(), service_names=other_names).version != base
    for name, value in (
        ("SOURCE_WORDS", (*cj.SOURCE_WORDS, "table of contents")),
        ("GENERIC_NAMES", cj.GENERIC_NAMES | {"foo"}),
        ("RULE_FAILS", ("term",)),
    ):
        with monkeypatch.context() as patched:
            patched.setattr(cj, name, value)
            assert grader().version != base, name
    note = grader(gap_types=True).run_note()
    assert note["gap_types"] is True and note["service_names_sha256"] == names().sha256
    assert "gaps_term" in note["counts"] and "names_cleared" in note["counts"]


def test_the_committed_service_name_file_covers_every_guide_and_reads_by_url() -> None:
    held = ServiceNames.load(ROOT / "configs" / "service-names.json")
    raw = json.loads((ROOT / "configs" / "service-names.json").read_text(encoding="utf-8"))
    assert (
        held.publisher == ("AWS", "Amazon") and len(held.segments) == 102 and len(held.pages) == 13
    )
    assert all(entries for entries in held.segments.values())
    sources = {e["source"] for held_ in raw["segments"].values() for e in held_}
    assert sources == {"title", "short", "abbreviation", "hand"}
    assert held.for_url(BEDROCK_URL) == ("Amazon Bedrock", "Bedrock")
    assert held.for_url(GENERAL_URL) == ("Amazon Bedrock", "Bedrock")  # by page, not segment
    assert held.for_url("https://docs.aws.amazon.com/general/latest/gr/unknown.html") == ()
    assert (
        held.for_url("https://docs.aws.amazon.com/awsec2/latest/x.html") == held.segments["AWSEC2"]
    )
    assert "SageMaker" in held.for_url("https://docs.aws.amazon.com/sagemaker/latest/dg/x.html")
    assert "AWS Identity and Access Management" in held.for_url(
        "https://docs.aws.amazon.com/IAM/latest/UserGuide/x.html"
    )
    assert held.cited([BEDROCK_URL, "https://docs.example.test/s3/x.html"]) == [
        "Amazon Bedrock",
        "Bedrock",
        "AWS",
        "Amazon",
    ]
    assert ServiceNames.load().sha256 == held.sha256  # the default path is the repository's file
    assert normalize("Bedrock") == "bedrock"
