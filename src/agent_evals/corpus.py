"""The documentation snapshot the docs agent searches, and the text graders check against.

Search is BM25 over heading-delimited chunks: no embedding service, fully deterministic.
Swapping in embeddings later is a change you measure, not a default you assume.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel
from rank_bm25 import BM25Plus

from agent_evals.harness.cache import sha256_text

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "which",
        "with",
        "you",
        "your",
    ]
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_TYPOGRAPHY = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"[*_`]")  # removed: they sit inside words ("**63**", "max_items")
_MD_STRUCTURE = re.compile(r"[#>|\[\]]")  # replaced with a space: they separate words
_MAX_CHUNK_CHARS = 1500


class Page(BaseModel):
    url: str
    title: str
    text: str  # markdown
    fetched_at: str
    sha256: str


@dataclass(frozen=True)
class Chunk:
    url: str
    title: str
    heading: str
    text: str


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    heading: str
    snippet: str
    score: float


def normalize(text: str) -> str:
    """Normalize text for quote matching: unicode, quotes, dashes, markdown, whitespace, case."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TYPOGRAPHY)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _MD_STRUCTURE.sub(" ", text)
    return " ".join(text.split()).lower()


def phrase_in(normalized_text: str, phrase: str) -> bool:
    """Whole-phrase match of a short key phrase in already-normalized text.

    No letter or digit may sit directly before the phrase when it starts with one, nor directly
    after it when it ends with one. So "5 MB" is not found inside "15 MB" and "10" not inside
    "100", while "/16" is still found in "10.0.0.0/16", "10%" in "about 10%." and "63" in
    "3-63". Used by the answer grader and by `evals validate`, so they never disagree.
    """
    needle = normalize(phrase)
    if not needle:
        return False
    pattern = re.escape(needle)
    if needle[0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if needle[-1].isalnum():
        pattern = pattern + r"(?!\w)"
    return re.search(pattern, normalized_text) is not None


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def chunk_page(page: Page) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = page.title
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        # Split long sections on paragraph boundaries.
        part: list[str] = []
        size = 0
        for para in body.split("\n\n"):
            if size + len(para) > _MAX_CHUNK_CHARS and part:
                chunks.append(Chunk(page.url, page.title, heading, "\n\n".join(part)))
                part, size = [], 0
            part.append(para)
            size += len(para)
        if part:
            chunks.append(Chunk(page.url, page.title, heading, "\n\n".join(part)))

    for line in page.text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            heading = line.lstrip("# ").strip() or heading
        else:
            buf.append(line)
    flush()
    return chunks


class Corpus:
    def __init__(self, pages: list[Page]) -> None:
        self.pages: dict[str, Page] = {p.url: p for p in pages}
        self.chunks: list[Chunk] = [c for p in pages for c in chunk_page(p)]
        self._normalized: dict[str, str] = {p.url: normalize(p.text) for p in pages}

    @classmethod
    def load(cls, directory: Path) -> Corpus:
        path = directory / "pages.jsonl"
        if not path.exists():
            raise FileNotFoundError(
                f"No snapshot at {path}. If you are not in the project folder (the one with "
                "pyproject.toml), cd there first; otherwise run `evals snapshot` to build one."
            )
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls([Page.model_validate(json.loads(line)) for line in lines if line.strip()])

    @cached_property
    def _bm25(self) -> BM25Plus:
        return BM25Plus([tokenize(f"{c.title} {c.heading} {c.text}") for c in self.chunks])

    @property
    def fingerprint(self) -> str:
        return sha256_text("\n".join(sorted(p.sha256 for p in self.pages.values())))

    def search(self, query: str, k: int = 5, snippet_chars: int = 300) -> list[SearchHit]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        scores = self._bm25.get_scores(terms)
        ranked = sorted(range(len(self.chunks)), key=lambda i: scores[i], reverse=True)
        hits: list[SearchHit] = []
        for i in ranked[:k]:
            c = self.chunks[i]
            snippet = " ".join(c.text.split())[:snippet_chars]
            hits.append(SearchHit(c.url, c.title, c.heading, snippet, float(scores[i])))
        return hits

    def read(self, url: str, start: int = 0, max_chars: int = 8000) -> str | None:
        page = self.pages.get(url)
        if page is None:
            return None
        text = page.text[start : start + max_chars]
        end = start + len(text)
        if end < len(page.text):
            text += f"\n\n[Truncated. {len(page.text) - end} more characters; call read_page "
            text += f"with start={end} to continue.]"
        return f"# {page.title}\nURL: {page.url}\n\n{text}"

    def contains_quote(self, url: str, quote: str) -> bool:
        """Verbatim quote check after normalization. A quote that normalizes to nothing (only
        markdown markers or whitespace) is on no page: the empty string is in every string, and
        that must not count."""
        haystack = self._normalized.get(url)
        needle = normalize(quote)
        return bool(needle) and haystack is not None and needle in haystack

    def contains_phrase(self, url: str, phrase: str) -> bool:
        """Whole-phrase match of a short key phrase on a page (see `phrase_in`)."""
        haystack = self._normalized.get(url)
        return haystack is not None and phrase_in(haystack, phrase)

    def page_chunks(self, url: str, query: str) -> list[Chunk]:
        """One page's chunks, best match for the query first, by the BM25 scores `search` uses."""
        indexes = [i for i, c in enumerate(self.chunks) if c.url == url]
        terms = tokenize(query)
        if not terms or not indexes:
            return [self.chunks[i] for i in indexes]
        scores = self._bm25.get_scores(terms)
        return [self.chunks[i] for i in sorted(indexes, key=lambda i: -scores[i])]
