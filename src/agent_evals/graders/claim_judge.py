"""The claim judge (judge v5, docs/plans/judge-v5.md): a claim table that code checks.

One call lists the answer's claims, each with the words of the answer that make it, its role
(core or peripheral), the quote spans that support it and a score from 0 to 100, plus the words
that make no claim. Code checks every span and flags what the table may have got wrong: a number
or a capitalized name that no claim covers, one among the words of no claim, and a claim's
number that none of its quote spans holds. A second, skeptical call scores the flagged items on
the same scale and names the one claim most likely unsupported. The verdict is a rule over all
of it at two thresholds, t for support and s for the skeptic's search, both in the version.

The judge sees the question, the answer and the numbered quotes: never the gold answer, and
never a URL, neither a citation's nor one inside a quote's text. A reply that fails validation is
asked again once under a second salt; when that fails too, the answer gets a `judge_error` grade
instead of verdicts, so a day can always be read. Like JudgeGrader, it skips errored, abstained
and uncited answers, and every call goes through LLMClient, so it is cached, timed and counted.
The grader also counts what its checks find in the answers it judges: retries, judge errors,
support entries dropped by the span check, and the verdicts that fail only because of one, which
`evals judge` reports and records.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_evals.config import JudgeConfig
from agent_evals.graders.judge_prompts import (
    CLAIMS_MESSAGE,
    FLAGS,
    ITEM_LINE,
    NO_ITEMS,
    QUOTE_LINE,
    SKEPTIC_MESSAGE,
    SUPPORT_RUBRICS,
    claims_message,
    claims_prompt,
    claims_tool,
    skeptic_message,
    skeptic_prompt,
    skeptic_tool,
)
from agent_evals.harness.cache import canonical_json, sha256_text
from agent_evals.harness.llm import LLMClient, usage_from
from agent_evals.types import AgentResult, Case, Grade, Usage

# The grid stage A chooses t and s from. Any other value is a new judge that needs new labels, so
# the grader refuses it. An s of None turns the skeptic's search off.
T_GRID = (50, 70, 85)
S_GRID: tuple[int | None, ...] = (None, 70, 90)
# The skeptic is asked about every answer that passes at the grid's lowest t, and every one with
# a flagged item, whatever t is configured: so one request and its cached reply serve every t.
SKEPTIC_GATE = min(T_GRID)
MIN_QUOTE_SPAN_WORDS = 3  # so that one common word cannot stand for support
# The version holds the rules' own data (the typography map, every pattern, the abbreviations,
# the span boundaries and the two numbers above). Bump RULES_VERSION when the logic of normalize(),
# the span checks or the coverage rules changes, and GRADING_CODE_VERSION when anything else that
# turns replies into messages or verdicts does.
RULES_VERSION = 2
GRADING_CODE_VERSION = 2

Role = Literal["core", "peripheral"]
FlagKind = Literal["uncovered", "not_claims", "number_not_quoted"]

_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",  # hyphen, which NFKC also makes of the non-breaking one
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2212": "-",  # minus sign
        "`": None,
        "*": None,  # markdown emphasis; a list item's "*" is read before it goes (_lines)
        "|": " ",
    }
)
_WHITESPACE = re.compile(r"\s+")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
# A line that begins a list item (- * + or a bullet, or a number: 1. 2) (3)), and one that is a
# heading or a table row. Read on a line as written, before folding drops a "*" or a "|"; a list
# item is read once folded too, so that a bold marker such as **1.** counts.
_LIST_ITEM = re.compile(r"\s*(?:[-*+\u2022]|\(?\d{1,3}[.)])\s")
_WHOLE_LINE = re.compile(r"\s*(?:#{1,6}\s|\|)")


@dataclass(frozen=True)
class _Line:
    text: str  # the line folded
    start: int  # where it starts in the folded text
    opens: bool  # a sentence starts here: a list item, a heading or a table row, or a new block
    whole: bool  # a heading or a table row, which never wraps: its sentence ends with the line


def _lines(text: str) -> list[_Line]:
    """The lines of text, folded: NFKC, the typography map, whitespace collapsed. A line that
    folds to nothing (a blank one, or markup alone such as a code fence) is dropped and ends a
    block, so the line after it opens a sentence, as the text's first line does."""
    lines: list[_Line] = []
    start, gap = 0, True
    for raw in _LINE_BREAK.split(unicodedata.normalize("NFKC", text)):
        folded = _WHITESPACE.sub(" ", raw.translate(_TYPOGRAPHY)).strip()
        if not folded:
            gap = True
            continue
        whole = _WHOLE_LINE.match(raw) is not None
        item = _LIST_ITEM.match(raw) is not None or _LIST_ITEM.match(folded) is not None
        opens = gap or whole or item
        lines.append(_Line(folded, start, opens, whole))
        start += len(folded) + 1
        gap = False
    return lines


def _fold(text: str) -> str:
    """normalize() before the casefold, with each line break kept as one: the coverage rules need
    case, sentences and lines."""
    return "\n".join(line.text for line in _lines(text))


def normalize(text: str) -> str:
    """Text as spans are matched: NFKC, straight quotes, "-" for every dash, no backticks or
    asterisks, table pipes as spaces, whitespace collapsed, casefolded."""
    return _fold(text).replace("\n", " ").casefold()


# A markdown link or image, whose own text stays (with one level of brackets in it, and of
# parentheses in its target), and a bare URL, with the <> of an autolink.
_LINK = re.compile(r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:[^()]|\([^()]*\))*\)")
_URL = re.compile(r"(?i)<?\b(?:https?://|www\.)[^\s<>\"'`]*[^\s<>\"'`.,;:!?)\]]>?")


def without_links(quote: str) -> str:
    """A quote's text as both calls and the span check read it: every link's target and every
    bare URL taken out, a link's own text kept. A URL is not a quote, and a page's address can
    credit an attribution that no quote states."""
    return _URL.sub("", _LINK.sub(r"\1", quote))


# A span, a word or a number matches only whole: no letter or digit right before or after it,
# and no decimal point or thousands comma joining it to a digit. So "5 gb" is not in "15 gb",
# nor "1 hour" in "1.5 hours", "99" in "99.9", "2" in "2,048" or "3" in "s3".
_BEFORE = r"(?<![^\W_])(?<!\d[.,])"
_AFTER = r"(?![.,]\d)(?![^\W_])"


def _places(span: str, text: str) -> list[tuple[int, int]]:
    """Every place where the span, normalized, occurs whole in normalized text, as (start, end),
    overlapping places included. An empty span occurs nowhere: it would be in every text."""
    needle = normalize(span)
    if not needle:
        return []
    pattern = re.compile(_BEFORE + re.escape(needle) + _AFTER)
    places: list[tuple[int, int]] = []
    found = pattern.search(text)
    while found is not None:
        places.append(found.span())
        found = pattern.search(text, found.start() + 1)
    return places


def _occurs(span: str, text: str) -> bool:
    """The span occurs whole in normalized text (see _places)."""
    return bool(_places(span, text))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupportEntry(_Strict):
    quote: int
    quote_span: str


class Claim(_Strict):
    answer_span: str
    role: Role
    support: list[SupportEntry]
    score: int = Field(ge=0, le=100)  # the strict schema cannot hold the range, so it is here
    why: str


class ClaimTable(_Strict):
    """A submit_claims call as the judge made it."""

    claims: list[Claim]
    not_claims: list[str]


class ItemScore(_Strict):
    item: int
    score: int = Field(ge=0, le=100)
    not_a_claim: bool
    why: str


class Search(_Strict):
    found: bool
    answer_span: str
    role: Role
    why: str
    confidence: int = Field(ge=0, le=100)


class SkepticReply(_Strict):
    """A submit_skeptic call as the skeptic made it."""

    flagged: list[ItemScore]
    search: Search


class CheckedSupport(BaseModel):
    """One support entry and its check: why code dropped it, or None when it holds."""

    quote: int
    quote_span: str
    problem: str | None = None


class CheckedClaim(BaseModel):
    answer_span: str
    role: Role
    support: list[CheckedSupport]
    score: int
    why: str

    @property
    def quoted(self) -> bool:
        """At least one of its support entries holds."""
        return any(s.problem is None for s in self.support)

    def supported(self, t: int) -> bool:
        return self.score >= t and self.quoted


class FlaggedItem(BaseModel):
    """Something code flagged for the skeptic, numbered as the skeptic sees it."""

    number: int
    kind: FlagKind
    token: str  # the number or name, as the answer writes it
    context: str  # its sentence, the words of no claim it is in, or its claim's answer_span
    claim: int | None = None  # for number_not_quoted: the claim's number, from 1
    core: bool = False  # it belongs to a core claim, so it bears on core supported too


class ClaimVerdict(BaseModel):
    fully_supported: bool
    core_supported: bool
    support_score: float  # the lowest claim score over 100; 1.0 with no claims
    reason: str


class Assessment(BaseModel):
    """What the claim judge found in one answer, from which verdict() reads the verdicts at any
    t and s. With an error there is no verdict."""

    claims: list[CheckedClaim] = Field(default_factory=list)
    not_claims: list[str] = Field(default_factory=list)
    flagged: list[FlaggedItem] = Field(default_factory=list)
    skeptic: SkepticReply | None = None  # None when the skeptic was not asked
    dropped_support: int = 0  # support entries whose check failed
    unmatched_not_claims: int = 0  # not_claims entries not in the answer, which cover nothing
    search_span_missing: bool = False  # its words not in the answer even on retry: none found
    retries: int = 0
    error: str | None = None  # a reply that failed validation twice

    def verdict(self, t: int, s: int | None) -> ClaimVerdict:
        """The verdicts at support threshold t and skeptic threshold s (None: the search fails
        nothing). A claim is supported at a score of t or more with a support entry that holds.
        A flagged item is cleared by a skeptic score of t or more, or as no claim when it is a
        name: a number is cleared only by its score (_cleared). The search fails the answer at a
        confidence of s or more, and its core claims too when the claim it names is core. A
        claim's flagged number bears on core supported when the claim is core; an uncovered
        number or name bears on fully supported only."""
        if self.error is not None:
            raise ValueError(f"a judge error has no verdict: {self.error}")
        failures: list[tuple[bool, str]] = []  # (it fails core supported too, why)
        for claim in self.claims:
            if not claim.supported(t):
                failures.append((claim.role == "core", _claim_failure(claim)))
        scored = {r.item: r for r in self.skeptic.flagged} if self.skeptic else {}
        for item in self.flagged:
            result = scored.get(item.number)
            if not _cleared(item, result, t):
                failures.append((item.core, _item_failure(item, result)))
        search = self.skeptic.search if self.skeptic else None
        if (
            s is not None
            and search is not None
            and search.found
            and not self.search_span_missing
            and search.confidence >= s
        ):
            why = f'"{search.answer_span}" ({search.role}), the skeptic: {search.why}'
            failures.append((search.role == "core", why))
        fully = not failures
        core = fully or not any(fails_core for fails_core, _ in failures)
        reason = failures[0][1] if failures else "every claim is quoted"
        if self.flagged:
            reason += "; flagged: " + ", ".join(_flag_note(item) for item in self.flagged)
        lowest = min((claim.score for claim in self.claims), default=100)
        return ClaimVerdict(
            fully_supported=fully,
            core_supported=core,
            support_score=lowest / 100,
            reason=reason.strip(),
        )

    def dropped_span_flip(self, t: int, s: int | None) -> bool:
        """The answer fails fully supported at t and s only because the span check dropped a
        support entry: it fails as it is, yet passes with every dropped entry put back and the
        rest as it was, flagged items included. Dropped entries, and the verdicts that flip only
        because of one, are reported at every stage (docs/plans/judge-v5.md, part 1). A judge
        error has no verdict, so it never flips."""
        if self.error is not None or self.verdict(t, s).fully_supported:
            return False
        if not any(entry.problem is not None for claim in self.claims for entry in claim.support):
            return False  # nothing was dropped
        return self._restored().verdict(t, s).fully_supported

    def _restored(self) -> Assessment:
        """This assessment with every support entry the span check dropped put back."""
        claims = [
            claim.model_copy(
                update={"support": [e.model_copy(update={"problem": None}) for e in claim.support]}
            )
            for claim in self.claims
        ]
        return self.model_copy(update={"claims": claims})


class JudgingCounts(BaseModel):
    """What the claim judge's checks counted over some judgings, each one answer it judged (an
    errored, abstained or uncited answer is not one). The flips are read at one t and s."""

    model_config = ConfigDict(frozen=True)

    judgings: int = 0
    retried: int = 0  # judgings with a reply asked again
    judge_errors: int = 0  # judgings whose reply failed validation twice: no verdict
    support_entries: int = 0  # in the claim tables, before the span check
    dropped: int = 0  # support entries the span check dropped
    with_dropped: int = 0  # judgings that lost at least one
    search_missing: int = 0  # searches whose words were not in the answer, even on the retry
    dropped_span_flips: int = 0  # verdicts that fail only because of a dropped entry

    @classmethod
    def of(cls, found: Assessment, t: int, s: int | None) -> JudgingCounts:
        """One judging's counts, its flip read at t and s."""
        return cls(
            judgings=1,
            retried=int(found.retries > 0),
            judge_errors=int(found.error is not None),
            support_entries=sum(len(claim.support) for claim in found.claims),
            dropped=found.dropped_support,
            with_dropped=int(found.dropped_support > 0),
            search_missing=int(found.search_span_missing),
            dropped_span_flips=int(found.dropped_span_flip(t, s)),
        )

    def add(self, other: JudgingCounts) -> JudgingCounts:
        return JudgingCounts(
            **{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields}
        )


class ClaimJudgeGrader:
    """The claim judge. `assess` finds everything for one answer; `grade` reads its verdicts at
    the configured t and s. It never grades under "judge", the validated judge's name. It counts
    what its checks found in every answer it judges (`counts`, its flips at its own t and s), as
    it counts the usage of its calls."""

    def __init__(self, llm: LLMClient, config: JudgeConfig, *, salt: str = "judge") -> None:
        if config.method != "claims":
            raise ValueError(
                f"judge.method is {config.method!r}: ClaimJudgeGrader is the claim judge, and "
                "the verdict judge is JudgeGrader"
            )
        if config.grader_name == "judge":
            raise ValueError(
                "the claim judge never grades under 'judge', the validated judge's name; set "
                "judge.grader_name"
            )
        if config.rubric_version not in SUPPORT_RUBRICS:
            raise ValueError(
                f"unknown judge.rubric_version {config.rubric_version!r}; "
                f"known: {sorted(SUPPORT_RUBRICS)}"
            )
        if config.support_threshold not in T_GRID:
            raise ValueError(
                f"judge.support_threshold is {config.support_threshold}: the claim judge's t is "
                f"one of {_grid(T_GRID)}, stage A's grid; any other t is a new judge"
            )
        if config.skeptic_threshold not in S_GRID:
            raise ValueError(
                f"judge.skeptic_threshold is {config.skeptic_threshold}: the claim judge's s is "
                f"one of {_grid(S_GRID)}, stage A's grid; any other s is a new judge"
            )
        self.llm = llm
        self.config = config
        self.salt = salt
        self.name = config.grader_name
        rubric = SUPPORT_RUBRICS[config.rubric_version]
        self.claims_prompt = claims_prompt(rubric)
        self.skeptic_prompt = skeptic_prompt(rubric)
        self.claims_tool = claims_tool(config.strict_tools)
        self.skeptic_tool = skeptic_tool(config.strict_tools)
        self.usage = Usage()  # every call, retries included, for the cost line
        self.counts = JudgingCounts()  # every answer judged, for the run note
        self._lock = threading.Lock()  # for both: several threads may judge with one grader
        # Changes with the model, any word of the prompts, templates or tools, either threshold,
        # the rules' data and the rules and code versions. Not with pricing, salt or the grader
        # name.
        fingerprint = {
            "grader": "claim judge",
            "model": config.model.model_dump(mode="json", exclude={"pricing"}),
            "prompts": {"claims": self.claims_prompt, "skeptic": self.skeptic_prompt},
            "messages": {
                "claims": CLAIMS_MESSAGE,
                "skeptic": SKEPTIC_MESSAGE,
                "quote": QUOTE_LINE,
                "item": ITEM_LINE,
                "flags": FLAGS,
                "no_items": NO_ITEMS,
            },
            "tools": [self.claims_tool, self.skeptic_tool],
            "thresholds": {"t": config.support_threshold, "s": config.skeptic_threshold},
            "rules": _rules(),
            "code": {"version": GRADING_CODE_VERSION},
        }
        self.version = sha256_text(canonical_json(fingerprint))[:12]

    def run_note(self) -> dict[str, Any]:
        """What `evals judge` records beside the version: the method, both thresholds, the salts
        a reply that fails validation is asked again under, and what the checks counted over the
        answers this grader judged, its dropped-span flips at those thresholds."""
        with self._lock:
            counts = self.counts
        return {
            "method": "claims",
            "support_threshold": self.config.support_threshold,
            "skeptic_threshold": self.config.skeptic_threshold,
            "retry_salts": [
                self._salt(call, "<case id>") for call in ("claims-retry", "skeptic-retry")
            ],
            "counts": counts.model_dump(),
        }

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        assessment = self.assess(case, result)
        if assessment is None:
            return []
        if assessment.error is not None:  # not a verdict: counted, never read as a fail
            return [Grade(metric="judge_error", value=1.0, passed=False, reason=assessment.error)]
        v = assessment.verdict(self.config.support_threshold, self.config.skeptic_threshold)
        return [
            _verdict_grade("judge_core_supported", v.core_supported, v.reason),
            _verdict_grade("judge_supported", v.fully_supported, v.reason),
            Grade(metric="judge_support_score", value=v.support_score, reason=_lowest(assessment)),
        ]

    def assess(self, case: Case, result: AgentResult) -> Assessment | None:
        """Everything the claim judge finds in one answer, or None when there is nothing to
        judge: an errored, abstained or uncited answer. Bad model output never raises: a reply
        that fails validation twice comes back as the assessment's error. Each assessment is
        added to the grader's counts."""
        found = self._assess(case, result)
        if found is not None:
            one = JudgingCounts.of(
                found, self.config.support_threshold, self.config.skeptic_threshold
            )
            with self._lock:
                self.counts = self.counts.add(one)
        return found

    def _assess(self, case: Case, result: AgentResult) -> Assessment | None:
        """assess(), before the assessment is counted."""
        if result.error or bool(result.output.get("abstained", False)):
            return None
        raw = result.output.get("citations")
        citations = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        if not citations:
            return None
        answer = str(result.output.get("answer", ""))
        # Both calls and the span check read the same quote text, with no URL in it.
        quotes = [without_links(str(c.get("quote", ""))) for c in citations]
        text = normalize(answer)
        message = claims_message(case.question, answer, quotes)
        table = self._claims(message, text, self._salt("claims", case.id))
        retries = 0
        if isinstance(table, str):
            retries += 1
            again = self._claims(message, text, self._salt("claims-retry", case.id))
            if isinstance(again, str):
                error = f"the claim table failed twice: {table}; then {again}"
                return Assessment(retries=retries, error=error)
            table = again
        claims = [_checked(claim, quotes) for claim in table.claims]
        flagged = _flagged(case.question, answer, claims, table.not_claims)
        found = Assessment(
            claims=claims,
            not_claims=table.not_claims,
            flagged=flagged,
            dropped_support=sum(s.problem is not None for c in claims for s in c.support),
            unmatched_not_claims=sum(not _occurs(span, text) for span in table.not_claims),
            retries=retries,
        )
        # "Passes at the gate" reads the table alone: every claim supported at the grid's lowest t.
        if not flagged and not all(claim.supported(SKEPTIC_GATE) for claim in claims):
            return found  # it fails at every t of the grid, and nothing was flagged
        message = skeptic_message(
            case.question,
            answer,
            quotes,
            _skeptic_table(claims, table.not_claims),
            [item.model_dump() for item in flagged],
        )
        numbers = [item.number for item in flagged]
        reply = self._skeptic(message, numbers, self._salt("skeptic", case.id))
        if isinstance(reply, str) or not _search_holds(reply.search, text):
            first = reply if isinstance(reply, str) else "its search named words not in the answer"
            found.retries += 1
            reply = self._skeptic(message, numbers, self._salt("skeptic-retry", case.id))
            if isinstance(reply, str):
                found.error = f"the skeptic's reply failed twice: {first}; then {reply}"
                return found
        found.skeptic = reply
        found.search_span_missing = not _search_holds(reply.search, text)
        return found

    def _salt(self, call: str, case_id: str) -> str:
        """Each call's cache salt; a retry has its own, so it is a new sample, not the replay."""
        return f"{self.salt}#{call}#{case_id}"

    def _claims(self, message: str, text: str, salt: str) -> ClaimTable | str:
        """The claim table, or what is wrong with the reply: no call, a cut-off or malformed
        one, or an answer_span that is not in the answer."""
        data = self._call(self.claims_prompt, message, self.claims_tool, salt)
        if isinstance(data, str):
            return data
        try:
            table = ClaimTable.model_validate(data)
        except ValidationError as exc:
            return f"a malformed submit_claims call ({_first_error(exc)})"
        for claim in table.claims:
            if not _occurs(claim.answer_span, text):
                return f'an answer_span not in the answer ("{claim.answer_span}")'
        return table

    def _skeptic(self, message: str, numbers: list[int], salt: str) -> SkepticReply | str:
        """The skeptic's reply, or what is wrong with it: no call, a cut-off or malformed one,
        or item keys that are missing, unknown or given twice."""
        data = self._call(self.skeptic_prompt, message, self.skeptic_tool, salt)
        if isinstance(data, str):
            return data
        try:
            reply = SkepticReply.model_validate(data)
        except ValidationError as exc:
            return f"a malformed submit_skeptic call ({_first_error(exc)})"
        given = [r.item for r in reply.flagged]
        problems = [
            f"{what} {keys}"
            for what, keys in (
                ("missing item keys", sorted(set(numbers) - set(given))),
                ("unknown item keys", sorted(set(given) - set(numbers))),
                ("item keys given twice", sorted({n for n in given if given.count(n) > 1})),
            )
            if keys
        ]
        return "; ".join(problems) if problems else reply

    def _call(
        self, system: str, message: str, tool: dict[str, Any], salt: str
    ) -> dict[str, Any] | str:
        """One call's tool input, or what is wrong with the reply."""
        cfg = self.config.model
        # No forced tool_choice: claude-sonnet-5-5 rejects it (400). The system prompt asks for
        # the tool, and a reply without the call is retried, never read as a verdict.
        request: dict[str, Any] = {
            "model": cfg.name,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "tools": [tool],
            "messages": [{"role": "user", "content": message}],
        }
        if cfg.temperature is not None:
            request["temperature"] = cfg.temperature
        resp = self.llm.create(request, salt=salt)
        with self._lock:
            self.usage = self.usage.add(usage_from(resp))
        if resp.stop_reason == "max_tokens":  # a cut-off tool input parses as a partial one
            return f"a reply cut off at max_tokens={cfg.max_tokens}"
        if resp.stop_reason == "refusal":  # may hold a cut-off tool input too
            return "a refused reply"
        for block in resp.content:
            if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
                data = block.get("input")
                return data if isinstance(data, dict) else f"a malformed {tool['name']} call"
        return f"no {tool['name']} call (stop_reason={resp.stop_reason!r})"


def _grid(values: tuple[int | None, ...]) -> str:
    return ", ".join("null" if value is None else str(value) for value in values)


def _checked(claim: Claim, quotes: list[str]) -> CheckedClaim:
    support = [
        CheckedSupport(quote=e.quote, quote_span=e.quote_span, problem=_support_problem(e, quotes))
        for e in claim.support
    ]
    return CheckedClaim(
        answer_span=claim.answer_span,
        role=claim.role,
        support=support,
        score=claim.score,
        why=claim.why,
    )


def _support_problem(entry: SupportEntry, quotes: list[str]) -> str | None:
    """Why a support entry fails its check, or None when it holds."""
    if not 1 <= entry.quote <= len(quotes):
        return f"there is no quote {entry.quote}"
    if len(normalize(entry.quote_span).split()) < MIN_QUOTE_SPAN_WORDS:
        return f"shorter than {MIN_QUOTE_SPAN_WORDS} words"
    if not _occurs(entry.quote_span, normalize(quotes[entry.quote - 1])):
        return f"not in quote {entry.quote}"
    return None


def _search_holds(search: Search, text: str) -> bool:
    """The search found nothing, or the words it names are in the answer."""
    return not search.found or _occurs(search.answer_span, text)


_WORD = re.compile(r"\S+")
_LIST_NUMBER = re.compile(r"\(?\d{1,3}[.)]")  # a numbered list item's own marker: 1. 2) (3)
_LEFT_EDGE = re.compile(r"^[\W_]+")
_RIGHT_EDGE = re.compile(r"[\W_]+$")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
_CLOSERS = re.compile(r"[\"')\]]+$")
# Words that end in a period but end no sentence.
_ABBREVIATIONS = ("e.g.", "etc.", "i.e.", "vs.")


def _patterns() -> dict[str, re.Pattern[str]]:
    """Every pattern the rules use, by name, for the version."""
    return {
        "whitespace": _WHITESPACE,
        "line_break": _LINE_BREAK,
        "list_item": _LIST_ITEM,
        "whole_line": _WHOLE_LINE,
        "link": _LINK,
        "url": _URL,
        "word": _WORD,
        "list_number": _LIST_NUMBER,
        "left_edge": _LEFT_EDGE,
        "right_edge": _RIGHT_EDGE,
        "sentence_end": _SENTENCE_END,
        "closers": _CLOSERS,
    }


def _rules() -> dict[str, Any]:
    """The normalization, span and coverage rules as data, so that a change to any of them
    changes the version even when RULES_VERSION is not bumped. Read when a grader is made."""
    return {
        "version": RULES_VERSION,
        "typography": {chr(code): to for code, to in _TYPOGRAPHY.items()},
        "patterns": {name: pattern.pattern for name, pattern in _patterns().items()},
        "span_boundaries": [_BEFORE, _AFTER],
        "abbreviations": list(_ABBREVIATIONS),
        "min_quote_span_words": MIN_QUOTE_SPAN_WORDS,
        "skeptic_gate": SKEPTIC_GATE,
    }


@dataclass(frozen=True)
class _Word:
    text: str  # without the punctuation around it, as the answer writes it
    start: int  # where it is in the folded text
    end: int
    first: bool  # it starts a sentence or a list item
    sentence: str


def _ends_sentence(token: str) -> bool:
    """The token ends a sentence: it ends in ., ! or ?, perhaps inside closing quotes or
    brackets, and is not a common abbreviation (e.g. i.e. etc. vs.)."""
    if _SENTENCE_END.search(token) is None:
        return False
    return _CLOSERS.sub("", _LEFT_EDGE.sub("", token)).casefold() not in _ABBREVIATIONS


def _words(text: str) -> list[_Word]:
    """The words of text, sentence by sentence, placed in _fold(text). A sentence ends at a token
    that ends one; a line starts a new one only when it opens one (a list item, a heading, a
    table row, or the first line of a block) or the line before ended its sentence. Otherwise it
    goes on with the sentence of the line before, as a hard-wrapped line does. A line's numbered
    list marker is not a word: it numbers the item and claims nothing."""
    lines = _lines(text)
    folded = "\n".join(line.text for line in lines)
    sentences: list[list[tuple[int, str]]] = [[]]  # each token: where it starts, and its text
    for line in lines:
        if line.opens:
            sentences.append([])
        found = list(_WORD.finditer(line.text))
        if found and _LIST_NUMBER.fullmatch(found[0].group()):
            found = found[1:]
        for match in found:
            sentences[-1].append((line.start + match.start(), match.group()))
            if _ends_sentence(match.group()):
                sentences.append([])
        if line.whole:
            sentences.append([])
    return [word for tokens in sentences if tokens for word in _sentence_words(folded, tokens)]


def _sentence_words(folded: str, tokens: list[tuple[int, str]]) -> list[_Word]:
    """One sentence's words: each token without the punctuation around it, the first of them
    starting the sentence, and the sentence's text on one line."""
    start, (last, last_text) = tokens[0][0], tokens[-1]
    sentence = folded[start : last + len(last_text)].replace("\n", " ")
    words: list[_Word] = []
    for at, raw in tokens:
        core = _RIGHT_EDGE.sub("", _LEFT_EDGE.sub("", raw))
        if not core:
            continue
        begin = at + len(raw) - len(_LEFT_EDGE.sub("", raw))
        words.append(_Word(core, begin, begin + len(core), not words, sentence))
    return words


def _is_number(word: str) -> bool:
    """A number is a word that starts with a digit. A name with a digit in it, such as EC2, S3
    or IPv6, is a name."""
    return word[:1].isdigit()


def _is_name(word: _Word, question: str) -> bool:
    """A capitalized word that starts no sentence or list item, is not "I" (nor "I'm") and is
    not in the normalized question."""
    if word.first or not word.text[0].isupper() or word.text.split("'")[0] == "I":
        return False
    return not _occurs(normalize(word.text).removesuffix("'s"), question)


def _cleared(item: FlaggedItem, result: ItemScore | None, t: int) -> bool:
    """A flagged item passes on a skeptic score of t or more, the bar every claim meets, and the
    score is how well the quotes support it. As no claim, a name passes too, but a number never
    does: a quantity passes only on its support."""
    if result is None:
        return False
    if result.score >= t:
        return True
    return result.not_a_claim and not _is_number(item.token)


def _casefold_offsets(folded: str) -> list[int]:
    """Where each character of folded text lands once casefolded, which can lengthen one."""
    offsets = [0]
    for ch in folded:
        offsets.append(offsets[-1] + len(ch.casefold()))
    return offsets


def _flagged(
    question: str, answer: str, claims: list[CheckedClaim], not_claims: list[str]
) -> list[FlaggedItem]:
    """The items the skeptic must score. First every number or capitalized name of the answer
    that no claim's answer_span covers, flagged as among the words of no claim when a not_claims
    entry holds it; then every number in a claim's answer_span that none of the claim's quote
    spans that hold contains whole."""
    folded = _fold(answer)
    text = folded.replace("\n", " ").casefold()  # normalize(answer)
    at = _casefold_offsets(folded)
    claimed = [r for claim in claims for r in _places(claim.answer_span, text)]
    unclaimed = [(span, r) for span in not_claims for r in _places(span, text)]
    asked = normalize(question)
    items: list[FlaggedItem] = []
    seen: set[tuple[str, str, str, int | None]] = set()

    def flag(kind: FlagKind, token: str, context: str, claim: int | None, core: bool) -> None:
        key = (kind, normalize(token), context, claim)
        if key not in seen:  # the same word twice in one place is one item
            seen.add(key)
            items.append(
                FlaggedItem(
                    number=len(items) + 1,
                    kind=kind,
                    token=token,
                    context=context,
                    claim=claim,
                    core=core,
                )
            )

    for word in _words(answer):
        if not (_is_number(word.text) or _is_name(word, asked)):
            continue
        lo, hi = at[word.start], at[word.end]
        if any(a <= lo and hi <= b for a, b in claimed):
            continue
        span = next((s for s, (a, b) in unclaimed if a <= lo and hi <= b), None)
        if span is None:
            flag("uncovered", word.text, word.sentence, None, False)
        else:
            flag("not_claims", word.text, span, None, False)
    for n, claim in enumerate(claims, 1):
        quoted = " ".join(normalize(s.quote_span) for s in claim.support if s.problem is None)
        for word in _words(claim.answer_span):
            if _is_number(word.text) and not _occurs(word.text, quoted):
                flag("number_not_quoted", word.text, claim.answer_span, n, claim.role == "core")
    return items


def _skeptic_table(claims: list[CheckedClaim], not_claims: list[str]) -> dict[str, Any]:
    """The claim table as code kept it: numbered, with only the support entries that hold."""
    return {
        "claims": [
            {
                "claim": n,
                "answer_span": claim.answer_span,
                "role": claim.role,
                "support": [
                    {"quote": s.quote, "quote_span": s.quote_span}
                    for s in claim.support
                    if s.problem is None
                ],
                "score": claim.score,
                "why": claim.why,
            }
            for n, claim in enumerate(claims, 1)
        ],
        "not_claims": not_claims,
    }


_FLAG_NOTES = {
    "uncovered": "in no claim",
    "not_claims": "in words of no claim",
    "number_not_quoted": "in none of claim {claim}'s quote spans",
}


def _flag_note(item: FlaggedItem) -> str:
    return f'"{item.token}" ' + _FLAG_NOTES[item.kind].format(claim=item.claim)


def _claim_failure(claim: CheckedClaim) -> str:
    if not claim.support:
        why = "it cites no quote"
    elif not claim.quoted:
        why = "no quote span of it holds: " + "; ".join(str(s.problem) for s in claim.support)
    else:
        why = f"score {claim.score}: {claim.why}"
    return f'"{claim.answer_span}" ({claim.role}), {why}'


def _item_failure(item: FlaggedItem, result: ItemScore | None) -> str:
    if result is None:
        return f"{_flag_note(item)}, with no skeptic score"
    return f"{_flag_note(item)}, skeptic score {result.score}: {result.why}"


def _lowest(assessment: Assessment) -> str:
    """For audit: the claim the support score comes from."""
    if not assessment.claims:
        return "no claims"
    claim = min(assessment.claims, key=lambda c: c.score)
    return f'the lowest claim score, {claim.score}: "{claim.answer_span}"'


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"])
    return f"{where}: {error['msg']}" if where else error["msg"]


def _verdict_grade(metric: str, ok: bool, reason: str) -> Grade:
    return Grade(metric=metric, value=float(ok), passed=ok, reason=reason.strip())
