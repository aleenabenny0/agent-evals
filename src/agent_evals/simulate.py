"""Simulated user for online evals (phase 7, docs/plans/phase-7-online-evals.md part 1).

One simulated day is a fixed number of questions with the same mix every day: each persona writes
the same quota of each intent, so days differ in their questions, not their composition.

The frozen test split stays clean. An in-snapshot question comes from one source page, and a page
is never a source if it is gold evidence for a test case or holds a test case's support sentence
word for word. A lookup or comparison is written from one section of its page, and a checker
model confirms that the section answers every part of it before it is kept (generator v2; v1 saw
only the title and headings, and the snapshot fully answered 6 of its 20 piloted lookups and
comparisons). An outside-snapshot question names a service that no snapshot page mentions (in
its title, URL or text) and no test question names, so declining it is known to be right and it
cannot echo a test case. A false premise contradicts one prose sentence from its page, kept as its
minimal key; it becomes the online case's `gold.support` when online cases are built (part 2).

Every generator call is cached under a salt built from the day, the slot, the page redraw and the
attempt, so a day can be rebuilt exactly, and `evals simulate` refuses to overwrite a day with
different questions.
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_evals.corpus import Chunk, Corpus, Page, tokenize
from agent_evals.harness.cache import sha256_text
from agent_evals.harness.llm import LLMClient
from agent_evals.types import Area

PERSONAS: dict[str, str] = {
    "new-developer": (
        "a developer in their first month on AWS: polite, unsure of the right terms, and writing "
        "full sentences"
    ),
    "on-call": (
        "an on-call engineer in the middle of an incident: terse, in a hurry, sometimes pasting an "
        "error message, with the odd typo and no punctuation at the end"
    ),
    "security-reviewer": (
        "a security reviewer checking a design: precise, interested in defaults, permissions, "
        "encryption and who can see what"
    ),
    "cost-manager": (
        "an engineering manager watching the bill: plain language, interested in what is charged, "
        "limits and whether something is worth turning on"
    ),
    "non-native": (
        "an engineer who writes English as a second language: short sentences, unusual word order "
        "and the occasional wrong preposition, but technically sharp"
    ),
}
# Per persona per day: 14 + 2 + 2 + 2 = 20, so five personas give 100 questions at 70/10/10/10.
QUOTA: dict[str, int] = {"lookup": 14, "comparison": 2, "false_premise": 2, "outside_snapshot": 2}
SYMPTOM_PER_PERSONA = 4  # of the lookup questions, phrased as a problem the person hit
OUTSIDE_PER_DAY = QUOTA["outside_snapshot"] * len(PERSONAS)  # each names a different service
CASE_TYPE = {
    "lookup": "lookup",
    "comparison": "multi_step",
    "false_premise": "false_premise",
    "outside_snapshot": "unanswerable",
}
# Candidates for outside-snapshot questions on top of suites.held_out_services. Several of those
# names are mentioned in passing on snapshot pages or named by test questions, which leaves fewer
# than the ten distinct services a day needs. Every candidate is re-checked against the snapshot
# and the test questions when a day is planned (`outside_services`).
EXTRA_OUTSIDE_SERVICES = (
    "Location Service",
    "IoT SiteWise",
    "MediaLive",
    "HealthLake",
    "Device Farm",
    "Managed Grafana",
    "Clean Rooms",
    "Wickr",
)
AREA_MAP_PATH = Path("configs/online-area-map.json")
DUPLICATE_JACCARD = 0.6
MAX_ATTEMPTS = 6  # per source page, each under its own sub-salt
MAX_REDRAWS = 2  # then an in-snapshot slot's page is redrawn, at most this many times
GENERATOR_VERSION = "v2"
MIN_SECTION_CHARS = 300  # shorter sections are mostly fragments and one-line callouts

SYSTEM = """\
You write one question that a real person would ask an AWS documentation assistant.
You are {persona}.
Write the question in that person's own voice, about their own situation. They have not read
the documentation, so do not copy its phrases or its title, and do not mention that you were
shown any. Ask exactly one question, at most 40 words. Call submit_question with it."""

CHECK_SYSTEM = """\
You check whether a passage from AWS documentation answers a user's question. Judge only from the
passage, not from what you know. "full": the passage states the answer to every part of the
question. "partial": it answers some parts but not all. "none": it answers no part. In
"missing", name each part the passage does not answer, or leave it empty. Call submit_check."""

_WORD = re.compile(r"[a-z0-9]+")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|etc|vs|approx|U\.S)\.$", re.I)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LIST_ITEM = re.compile(r"\s*(?:[*+-]|\d+[.)])\s")
_QUANTITY = re.compile(
    r"\b\d[\d,.]*\s*(?:%|percent\b|(?:[KMGTP]i?B|bytes?|ms|milliseconds?|seconds?|minutes?|"
    r"hours?|days?|weeks?|months?|years?|requests?|calls?|transactions?|messages?|items?|"
    r"objects?|characters?|attempts?|retries|times|vCPUs?|connections?|partitions?|versions?|"
    r"tags?|rules?|users?|roles?|keys?)\b)",
    re.I,
)
_CONSTRAINT = re.compile(
    r"\b(?:by default|default|maximum|minimum|at most|at least|up to|cannot|can't|can not|must|"
    r"only|not supported|isn't supported|doesn't support|does not support|no longer)\b",
    re.I,
)
_MARKUP = ("`", "|", "{", "}", "*", "<", "http")
# Sentences that point elsewhere rather than state a fact ("..., see Quotas.", "In this diagram").
_POINTER = re.compile(
    r"(?:^|, )see\b|\bfollowing\b|^for (?:more information|a complete list)|"
    r"\bthis (?:diagram|figure|example|table|section|page|topic|procedure|tutorial)\b",
    re.I,
)
_NOISE_HEADINGS = frozenset({"note", "important", "tip", "warning", "topics", "contents"})


class TrafficQuestion(BaseModel):
    """One simulated question as stored in results/online/traffic/day-NN.jsonl."""

    model_config = ConfigDict(extra="forbid")

    id: str
    day: int
    persona: str
    intent: str
    type: str
    question: str
    source_url: str | None = None
    service: str | None = None
    area: str | None = None
    symptom: bool = False
    # False premise: the page sentence the question contradicts; the online case's gold.support.
    contradicts: str | None = None
    # Lookup or comparison: the section of its page it was written from, which the checker found
    # answers every part of it.
    section: str | None = None
    title_overlap: float = 0.0
    copied_run: int = 0  # the longest run of words it shares with its page
    attempt: int = 0
    redraw: int = 0  # how many times the slot's source page was redrawn


@dataclass(frozen=True)
class Slot:
    day: int
    index: int
    persona: str
    intent: str
    source_url: str | None
    service: str | None
    sentence: str | None
    symptom: bool
    section: str | None = None  # lookup or comparison: "heading\n\ntext" of one page section


class _Submitted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str


class _Checked(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["full", "partial", "none"]
    missing: str


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def mentions(text: str, name: str) -> bool:
    """Whole words, any case: "Braket" is in "Amazon Braket's" but not in "Brakets"."""
    pattern = rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def headings(page: Page) -> list[str]:
    """Section headings outside code fences, without labels such as "Note"."""
    out: list[str] = []
    fenced = False
    for line in page.text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading and heading.lower() not in _NOISE_HEADINGS:
                out.append(heading)
    return out


def title_overlap(question: str, page: Page) -> float:
    """Share of the question's content words (stopwords dropped) also in the title or headings."""
    q = set(tokenize(question))
    t = set(tokenize(" ".join([page.title, *headings(page)])))
    return len(q & t) / len(q) if q else 0.0


def service_of(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    return parts[0] if parts else ""


def load_area_map(path: Path = AREA_MAP_PATH) -> dict[str, dict[str, str]]:
    """The committed area map; every value must be one of the case areas (`types.Area`)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {"pages": dict(data.get("pages", {})), "services": dict(data.get("services", {}))}
    unknown = {a for m in out.values() for a in m.values()} - set(get_args(Area))
    if unknown:
        raise ValueError(f"area map uses unknown areas: {sorted(unknown)}")
    return out


def page_areas(area_map: Mapping[str, Mapping[str, str]], urls: Iterable[str]) -> dict[str, str]:
    """Each page's area: its own entry in the map, else its service's (the first URL path
    segment). Raises if any page has neither, so the area mix never has a gap."""
    pages, services = area_map.get("pages", {}), area_map.get("services", {})
    out: dict[str, str] = {}
    missing: list[str] = []
    for url in urls:
        area = pages.get(url) or services.get(service_of(url))
        if area:
            out[url] = area
        else:
            missing.append(url)
    if missing:
        raise ValueError(f"no area for {len(missing)} page(s) in the area map: {missing[:3]}")
    return out


def protected_pages(corpus: Corpus, test_cases: Iterable[Any]) -> dict[str, set[str]]:
    """Pages no question may come from, by reason: the gold evidence pages of test cases, and the
    other pages that hold a test case's support sentence word for word."""
    cases = list(test_cases)
    evidence = {u for c in cases for group in c.gold.evidence for u in group if u in corpus.pages}
    support = {
        url
        for c in cases
        for sentence in c.gold.support
        for url in corpus.pages
        if url not in evidence and corpus.contains_quote(url, sentence)
    }
    return {"test_evidence": evidence, "test_support_sentence": support}


def outside_services(
    candidates: Iterable[str], corpus: Corpus, test_questions: Iterable[str]
) -> tuple[list[str], dict[str, str]]:
    """Services an outside-snapshot question may name: on no snapshot page (title, URL or text)
    and in no test question. Returns them in order, and each dropped name with its reason."""
    pages = [f"{p.title}\n{p.url}\n{p.text}" for p in corpus.pages.values()]
    tests = list(test_questions)
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for name in dict.fromkeys(candidates):
        if any(mentions(text, name) for text in pages):
            dropped[name] = "named on a snapshot page"
        elif any(mentions(q, name) for q in tests):
            dropped[name] = "named by a test question"
        else:
            kept.append(name)
    return kept, dropped


def prose(text: str) -> list[str]:
    """Paragraphs of running prose with hard wraps joined. Skips fenced code, headings, tables,
    lists, quotes, definition lists and indented blocks."""
    blocks: list[list[str]] = [[]]
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            blocks.append([])
        elif fenced or not line.strip():
            if blocks[-1]:
                blocks.append([])
        else:
            blocks[-1].append(line)
    out: list[str] = []
    for block in blocks:
        if not block or block[0] != block[0].lstrip():  # indented: code or a list continuation
            continue
        if block[0].startswith(("#", "|", ">", ":", "*", "-", "+")) or _LIST_ITEM.match(block[0]):
            continue
        if any(ln.lstrip().startswith(("|", ":")) or _LIST_ITEM.match(ln) for ln in block[1:]):
            continue  # a list or table runs on after the first line
        out.append(" ".join(ln.strip() for ln in block))
    return out


def sentences(paragraph: str) -> list[str]:
    """Split on sentence ends, never after "e.g." and the like."""
    out: list[str] = []
    for piece in _SPLIT.split(paragraph):
        if out and _ABBREVIATION.search(out[-1]):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return [s.strip() for s in out if s.strip()]


def factual_sentences(corpus: Corpus, url: str) -> list[str]:
    """Prose sentences on a page that state a checkable fact, for a false premise to contradict:
    a number with a unit, or a default, limit or prohibition. Links become their text; anything
    left with code, table or emphasis markup is skipped, and every sentence is checked to be on
    the page word for word."""
    page = corpus.pages.get(url)
    if page is None:
        return []
    out: list[str] = []
    for paragraph in prose(page.text):
        for s in sentences(_MD_LINK.sub(r"\1", paragraph)):
            if not 40 <= len(s) <= 300 or len(s.split()) < 8:
                continue
            if not (s[0].isupper() and s.endswith(".")) or any(m in s for m in _MARKUP):
                continue
            if _POINTER.search(s):
                continue
            if not (_QUANTITY.search(s) or _CONSTRAINT.search(s)):
                continue
            if corpus.contains_quote(url, s):
                out.append(s)
    return out


def factual_sentence(corpus: Corpus, url: str, rng: random.Random) -> str | None:
    candidates = factual_sentences(corpus, url)
    return rng.choice(candidates) if candidates else None


def key_sections(corpus: Corpus, url: str) -> list[Chunk]:
    """Sections of a page that a lookup or comparison can rest on: substantive text, not a callout
    such as "Note", a list of links, mostly code, or a fragment."""
    out: list[Chunk] = []
    for chunk in corpus.chunks:
        if chunk.url != url or chunk.heading.strip().lower() in _NOISE_HEADINGS:
            continue
        text = chunk.text.strip()
        if len(text) < MIN_SECTION_CHARS or text.count("](") * 80 > len(text):
            continue  # a fragment, or more than one link per 80 characters
        code = sum(len(block) for block in re.findall(r"```.*?```", text, re.S))
        if 2 * code <= len(text):
            out.append(chunk)
    return out


def _draw_page(
    intent: str, corpus: Corpus, pool: Sequence[str], rng: random.Random
) -> tuple[str, str | None, str | None]:
    """A source page and what the question rests on: a factual sentence for a false premise, one
    section for a lookup or comparison. A page without one is replaced by another draw."""
    for _ in range(50):
        url = rng.choice(pool)
        if intent == "false_premise":
            if sentence := factual_sentence(corpus, url, rng):
                return url, sentence, None
        elif sections := key_sections(corpus, url):
            chunk = rng.choice(sections)
            return url, None, f"{chunk.heading}\n\n{chunk.text.strip()}"
    raise ValueError(f"no page with something a {intent} question can rest on")


def plan_day(
    day: int,
    corpus: Corpus,
    excluded_urls: set[str],
    outside: Sequence[str],
) -> list[Slot]:
    """Fixed quotas per persona. Source pages are drawn uniformly from the pages not excluded; the
    outside-snapshot slots take distinct services, in an order shuffled per day."""
    rng = random.Random(f"online-day-{day}")
    pool = sorted(url for url in corpus.pages if url not in excluded_urls)
    if not pool:
        raise ValueError("no allowed source pages to plan a day from")
    services = list(dict.fromkeys(outside))
    if len(services) < OUTSIDE_PER_DAY:
        raise ValueError(
            f"need {OUTSIDE_PER_DAY} distinct outside-snapshot services, got {len(services)}"
        )
    rng.shuffle(services)
    slots: list[Slot] = []
    n_outside = 0
    for persona in PERSONAS:
        symptoms = set(rng.sample(range(QUOTA["lookup"]), SYMPTOM_PER_PERSONA))
        for intent, n in QUOTA.items():
            for k in range(n):
                index = len(slots)
                if intent == "outside_snapshot":
                    service = services[n_outside]
                    n_outside += 1
                    slots.append(Slot(day, index, persona, intent, None, service, None, False))
                    continue
                url, sentence, section = _draw_page(intent, corpus, pool, rng)
                symptom = intent == "lookup" and k in symptoms
                slots.append(
                    Slot(day, index, persona, intent, url, None, sentence, symptom, section)
                )
    return slots


def _redraw(slot: Slot, corpus: Corpus, pool: Sequence[str], redraw: int) -> Slot:
    """The same slot (persona, intent) on another page, drawn from its own seed."""
    rng = random.Random(f"online-day-{slot.day}-slot-{slot.index}-redraw-{redraw}")
    others = [u for u in pool if u != slot.source_url] or list(pool)
    url, sentence, section = _draw_page(slot.intent, corpus, others, rng)
    return dataclasses.replace(slot, source_url=url, sentence=sentence, section=section)


def generation_request(
    slot: Slot, page: Page | None, model: str, hint: str | None = None
) -> dict[str, Any]:
    if slot.intent == "outside_snapshot":
        task = (
            f"Ask one specific, practical question about {slot.service}, an AWS service, and "
            f"name {slot.service} in it. Ask about {slot.service} itself, not about how it works "
            "with other AWS services."
        )
    elif slot.intent == "false_premise":
        assert page is not None
        topic = (
            f"The documentation page is titled '{page.title}'. Its sections include: "
            + "; ".join(headings(page)[:12])
            + "."
        )
        ask = (
            f'Here is a sentence from the page: "{slot.sentence}" Ask a question that wrongly '
            "assumes the opposite of that sentence, the way a confused user would, without "
            "saying you are unsure. Ask nothing that the sentence does not settle."
        )
        task = f"{topic}\n{ask}"
    else:
        assert page is not None and slot.section, "a lookup or comparison needs a section"
        topic = (
            f"The documentation page is titled '{page.title}'. Here is one section of it:\n"
            f"<section>\n{slot.section}\n</section>"
        )
        if slot.intent == "lookup":
            ask = (
                "Ask one specific question that this section answers completely: one thing "
                "only, and nothing the section does not say."
            )
            if slot.symptom:
                ask += (
                    " Phrase it as a problem you ran into, for example an error message or "
                    "something that behaved differently than you expected. The section must "
                    "still answer it completely."
                )
        else:
            ask = (
                "Ask a question comparing two options, settings or approaches that this section "
                "describes. The section must answer the whole comparison, so do not ask which "
                "one to choose unless it says how to choose."
            )
        task = f"{topic}\n{ask}"
    if hint:
        task += f"\n{hint}"
    tool: dict[str, Any] = {
        "name": "submit_question",
        "description": "Submit the question.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
        # Fixed here, not read from a config, so no config change can alter the generator's
        # requests and so its cache keys.
        "strict": True,
    }
    return {
        "model": model,
        "max_tokens": 1024,
        "system": SYSTEM.format(persona=PERSONAS[slot.persona]),
        "tools": [tool],
        "messages": [{"role": "user", "content": task}],
    }


def _ask(llm: LLMClient, request: dict[str, Any], salt: str) -> str:
    resp = llm.create(request, salt=salt)
    if resp.stop_reason == "max_tokens":
        raise ValueError("generator hit max_tokens before finishing its question")
    for block in resp.content:
        if block.get("type") == "tool_use" and block.get("name") == "submit_question":
            try:
                q = _Submitted.model_validate(block.get("input", {})).question.strip()
            except ValidationError as exc:
                raise ValueError(f"generator output failed validation: {exc}") from exc
            if q:
                return q
    raise ValueError(f"generator returned no question (stop_reason={resp.stop_reason!r})")


def check_request(question: str, section: str, model: str) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "submit_check",
        "description": "Submit the verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["full", "partial", "none"]},
                "missing": {"type": "string"},
            },
            "required": ["verdict", "missing"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    return {
        "model": model,
        "max_tokens": 2048,  # room to think before the tool call, as the judge needs
        "system": CHECK_SYSTEM,
        "tools": [tool],
        "messages": [
            {
                "role": "user",
                "content": f"<passage>\n{section}\n</passage>\n\nQuestion: {question}",
            }
        ],
    }


def _check(llm: LLMClient, request: dict[str, Any], salt: str) -> _Checked:
    resp = llm.create(request, salt=salt)
    if resp.stop_reason == "max_tokens":
        raise ValueError("checker hit max_tokens before its verdict")
    for block in resp.content:
        if block.get("type") == "tool_use" and block.get("name") == "submit_check":
            try:
                return _Checked.model_validate(block.get("input", {}))
            except ValidationError as exc:
                raise ValueError(f"checker output failed validation: {exc}") from exc
    raise ValueError(f"checker returned no verdict (stop_reason={resp.stop_reason!r})")


def longest_shared_run(question: str, text: str) -> int:
    """Length in words of the longest word sequence the question shares with the text."""
    q = _WORD.findall(question.lower())
    haystack = " " + " ".join(_WORD.findall(text.lower())) + " "
    best = 0
    for i in range(len(q)):
        for j in range(i + best + 1, len(q) + 1):  # only runs longer than the best so far
            if " " + " ".join(q[i:j]) + " " not in haystack:
                break
            best = j - i
    return best


def _fill(
    llm: LLMClient,
    slot: Slot,
    corpus: Corpus,
    model: str,
    earlier: Sequence[TrafficQuestion],
    pool: Sequence[str],
    redo: dict[str, int],
) -> tuple[Slot, str, int, int]:
    """One slot's question, its (possibly redrawn) slot, the attempt and the redraw count."""
    planned = slot
    for redraw in range(MAX_REDRAWS + 1):
        if redraw:
            if slot.source_url is None or not pool:
                break
            slot = _redraw(planned, corpus, pool, redraw)
            redo["redrawn_page"] += 1
        page = corpus.pages.get(slot.source_url) if slot.source_url else None
        base = f"online-day{slot.day:02d}-slot{slot.index:03d}"
        if redraw:
            base += f"-redo{redraw}"
        hint: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            request = generation_request(slot, page, model, hint)
            try:
                # sonnet-5-5 rejects a forced tool_choice (graders/judge.py), and a failed reply
                # is cached like any other, so a failure moves on to the next sub-salt.
                question = _ask(llm, request, f"{base}-try{attempt}")
            except ValueError:
                redo["failed"] += 1
                continue
            if slot.service and not mentions(question, slot.service):
                redo["unnamed_service"] += 1
                hint = f'Use the name "{slot.service}" in the question.'
                continue
            clash = next(
                (q.question for q in earlier if jaccard(question, q.question) >= DUPLICATE_JACCARD),
                None,
            )
            if clash is not None:
                redo["duplicate"] += 1
                hint = f'Someone already asked this, so ask something different: "{clash}"'
                continue
            if slot.section:
                try:
                    checked = _check(
                        llm,
                        check_request(question, slot.section, model),
                        f"{base}-try{attempt}-check",
                    )
                except ValueError:
                    redo["failed"] += 1
                    continue
                if checked.verdict != "full":
                    redo["unanswerable"] += 1
                    gap = f" ({checked.missing})" if checked.missing.strip() else ""
                    hint = (
                        f'The section does not answer every part of "{question}"{gap}. Ask only '
                        "what the section answers."
                    )
                    continue
            return slot, question, attempt, redraw
    raise ValueError(
        f"slot {planned.index} ({planned.intent}): no usable question after every retry; {redo}"
    )


def generate_day(
    llm: LLMClient,
    slots: Sequence[Slot],
    corpus: Corpus,
    areas: Mapping[str, str],
    model: str,
    pool: Sequence[str] = (),
) -> tuple[list[TrafficQuestion], dict[str, int]]:
    """Generate every slot in order. A slot is retried under a new sub-salt when the generator
    gives no question, when an outside-snapshot question leaves out its service, when the
    question is a near-duplicate of an earlier one that day (with the clash quoted back), or when
    the checker finds its section does not answer every part of it (with the gap named). After
    MAX_ATTEMPTS on one page an in-snapshot slot's page is redrawn from `pool`, keeping persona
    and intent. A day so gets its full quota or fails loudly. `areas` maps page URL to area.
    Returns the questions and the retry counts by reason."""
    out: list[TrafficQuestion] = []
    redo = {"failed": 0, "unnamed_service": 0, "duplicate": 0, "unanswerable": 0, "redrawn_page": 0}
    for planned in slots:
        slot, question, attempt, redraw = _fill(llm, planned, corpus, model, out, pool, redo)
        page = corpus.pages.get(slot.source_url) if slot.source_url else None
        out.append(
            TrafficQuestion(
                id=f"online-d{slot.day:02d}-{slot.index:03d}",
                day=slot.day,
                persona=slot.persona,
                intent=slot.intent,
                type=CASE_TYPE[slot.intent],
                question=question,
                source_url=slot.source_url,
                service=slot.service,
                area=areas.get(slot.source_url) if slot.source_url else None,
                symptom=slot.symptom,
                contradicts=slot.sentence,
                section=slot.section,
                title_overlap=round(title_overlap(question, page), 3) if page else 0.0,
                copied_run=longest_shared_run(question, page.text) if page else 0,
                attempt=attempt,
                redraw=redraw,
            )
        )
    return out, redo


def _count(values: Iterable[str]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for v in values:
        tally[v] = tally.get(v, 0) + 1
    return dict(sorted(tally.items()))


def day_summary(questions: Sequence[TrafficQuestion]) -> dict[str, Any]:
    inside = [q for q in questions if q.source_url]
    per_page = _count(q.source_url or "" for q in inside)
    return {
        "questions": len(questions),
        "by_intent": _count(q.intent for q in questions),
        "by_persona": _count(q.persona for q in questions),
        "by_area": _count(str(q.area) for q in inside),  # in-snapshot questions only
        "outside_services": sorted({s for q in questions if (s := q.service)}),
        "source_pages": len(per_page),
        "most_questions_from_one_page": max(per_page.values(), default=0),
        "symptom_phrased": sum(1 for q in questions if q.symptom),
        "mean_title_overlap": round(sum(q.title_overlap for q in inside) / len(inside), 3)
        if inside
        else 0.0,
        "copied_6_or_more_words": sum(1 for q in inside if q.copied_run >= 6),
        "mean_copied_run": round(sum(q.copied_run for q in inside) / len(inside), 2)
        if inside
        else 0.0,
        "regenerated": sum(1 for q in questions if q.attempt or q.redraw),
    }


def load_traffic(path: Path) -> list[TrafficQuestion]:
    return [
        TrafficQuestion.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- The pilot review (`evals pilot-review`) -------------------------------------------------

# Per persona, 6 questions stratified by intent, so every intent gets at least 5 reviews in all:
# a plain random 30 held one outside-snapshot question. The rare intents are over-sampled, so the
# realism bar weighs each intent's rate by its share of a day's traffic.
PILOT_PER_PERSONA: dict[str, int] = {
    "lookup": 3,
    "comparison": 1,
    "false_premise": 1,
    "outside_snapshot": 1,
}
TRAFFIC_SHARE = {intent: n / sum(QUOTA.values()) for intent, n in QUOTA.items()}
# The realism read of a revised generator (plan, "Pilot and generator v2"): one lookup and one
# comparison per persona, the two intents v2 changed, judged for realism only. Fixed before the
# read: at least QUICK_BAR of the 10 realistic, or the prompt is revised.
QUICK_PER_PERSONA: dict[str, int] = {"lookup": 1, "comparison": 1}
QUICK_BAR = 7
# The right answer to "could the snapshot answer it?" (for a false premise: show it wrong).
EXPECTED_ANSWERABLE = {
    "lookup": "y",
    "comparison": "y",
    "false_premise": "y",
    "outside_snapshot": "n",
}


def question_sha(q: TrafficQuestion) -> str:
    return sha256_text(q.question)[:16]


def pilot_sample(
    questions: Sequence[TrafficQuestion],
    day: int,
    per_persona: Mapping[str, int] = PILOT_PER_PERSONA,
    name: str = "pilot",
) -> list[TrafficQuestion]:
    """`per_persona` questions by intent from each persona, drawn reproducibly from a seed named
    after the review, in shuffled order."""
    rng = random.Random(f"{name}-{day}")
    picked: list[TrafficQuestion] = []
    for persona in PERSONAS:
        for intent, n in per_persona.items():
            cell = [q for q in questions if q.persona == persona and q.intent == intent]
            picked.extend(rng.sample(cell, min(n, len(cell))))
    rng.shuffle(picked)
    return picked


def load_verdicts(path: Path) -> dict[str, dict[str, Any]]:
    """Pilot verdicts by question id, the last per id winning. A torn line from an interrupted
    write is skipped rather than losing the rest."""
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "id" in rec:
            out[rec["id"]] = rec
    return out


def append_verdict(path: Path, record: Mapping[str, Any]) -> None:
    """Append one verdict on its own line. After a torn last line, a newline goes first, so the
    new verdict cannot fuse with the torn text and be skipped with it."""
    torn = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if torn else "") + json.dumps(record) + "\n")


def current_verdicts(
    picked: Sequence[TrafficQuestion], verdicts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """Verdicts on the picked questions as they are now; one given on a question since
    regenerated under the same id does not count."""
    return {
        q.id: v
        for q in picked
        if (v := verdicts.get(q.id)) is not None and v.get("question_sha") == question_sha(q)
    }


def pilot_summary(
    picked: Sequence[TrafficQuestion], verdicts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The pilot's rates over the current sample. The bar is the traffic-weighted realistic
    share, and it is decided only once every question is reviewed."""
    current = current_verdicts(picked, verdicts)
    by_intent: dict[str, dict[str, Any]] = {}
    for intent in QUOTA:
        asked = [q for q in picked if q.intent == intent]
        if not asked:
            continue
        recs = [current[q.id] for q in asked if q.id in current]
        by_intent[intent] = {
            "reviewed": len(recs),
            "of": len(asked),
            "realistic": sum(1 for r in recs if r["realistic"]),
            "answerable": {a: sum(1 for r in recs if r["answerable"] == a) for a in "ynu"},
            "answerable_expected": EXPECTED_ANSWERABLE[intent],
        }
    rated = {i: c for i, c in by_intent.items() if c["reviewed"]}
    weighted = None
    if rated:
        share = sum(TRAFFIC_SHARE[i] for i in rated)
        weighted = sum(TRAFFIC_SHARE[i] * c["realistic"] / c["reviewed"] for i, c in rated.items())
        weighted /= share
    reviewed = len(current)
    complete = bool(picked) and reviewed == len(picked)
    realistic = sum(c["realistic"] for c in by_intent.values())
    return {
        "reviewed": reviewed,
        "sample": len(picked),
        "complete": complete,
        "realistic": realistic,
        "realistic_share": round(realistic / reviewed, 3) if reviewed else None,
        "realistic_share_weighted": round(weighted, 3) if weighted is not None else None,
        # Decided on the unrounded share, and only on a finished pilot.
        "meets_two_thirds": weighted >= 2 / 3 - 1e-9 if complete and weighted is not None else None,
        "by_intent": by_intent,
    }


def quick_summary(
    picked: Sequence[TrafficQuestion], verdicts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The realism read: realistic count against QUICK_BAR, decided once all are reviewed."""
    current = current_verdicts(picked, verdicts)
    realistic = sum(1 for v in current.values() if v["realistic"])
    complete = bool(picked) and len(current) == len(picked)
    return {
        "reviewed": len(current),
        "sample": len(picked),
        "complete": complete,
        "realistic": realistic,
        "bar": QUICK_BAR,
        "passes": realistic >= QUICK_BAR if complete else None,
    }


def _window(text: str, terms: set[str], size: int) -> str:
    """The stretch of about `size` characters of a passage that holds the most query words."""
    flat = " ".join(text.split())
    if len(flat) <= size:
        return flat
    starts = [m.start() for m in re.finditer(r"\S+", flat)]
    best = max(starts, key=lambda s: len(set(tokenize(flat[s : s + size])) & terms))
    start = max(0, best - 80)  # a little context before the densest stretch
    piece = flat[start : start + size]
    return ("... " if start else "") + piece + (" ..." if start + size < len(flat) else "")


def source_passages(
    corpus: Corpus, q: TrafficQuestion, k: int = 2, size: int = 600
) -> list[tuple[str, str]]:
    """The k passages of the question's source page that best match it, ranked as the agent's
    search ranks them, each as (heading, its best stretch). They can still miss the answer."""
    if not q.source_url:
        return []
    terms = set(tokenize(q.question))
    chunks = corpus.page_chunks(q.source_url, q.question)[:k]
    return [(c.heading, _window(c.text, terms, size)) for c in chunks]
