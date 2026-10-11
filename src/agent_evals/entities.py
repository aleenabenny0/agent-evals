"""Numbers and URLs in text: the entities a deterministic grounding check can compare.

Shared by the grounding grader (is every figure in the answer on a page the agent saw?), the
suite validator (does a support quote cover every figure in the gold answer?) and the review
screen (where on the page do the answer's figures sit?). One extraction, so the three agree.
"""

from __future__ import annotations

import re

# A standalone number: digits with optional thousands separators and a decimal part. Nothing
# may touch it on the left (so "S3", "EC2", "python3.10" and "m5.large" shed no number) and on the
# right it may only be followed by a short unit stuck to it, as AWS docs write "1GB" and "10GB"
# (so "1GB" and "99.9%" count, "2xlarge" does not), by punctuation, or by the end. "10.0.0.0" is
# an address, not four numbers; a "63." ending a sentence is still 63.
NUMBER = re.compile(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)(?=[A-Za-z]{0,4}(?!\w)(?!\.\d))")
# Region and Availability Zone codes end in a digit that is a name, not a quantity.
_REGION_CODE = re.compile(r"\b[a-z]{2}(?:-gov|-iso[a-z]*)?-[a-z]+-\d[a-z]?\b")
# "1." or "2)" at the start of a line numbers a list item; it is not a claim about anything.
_LIST_MARKER = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_TRAILING_PUNCTUATION = ".,;:!?"


def numbers_in(text: str) -> set[str]:
    """Every standalone number in `text`, thousands separators removed ("10,240" -> "10240")."""
    text = _LIST_MARKER.sub(" ", _REGION_CODE.sub(" ", text))
    return {m.group(1).replace(",", "") for m in NUMBER.finditer(text)}


def normalize_url(url: str) -> str:
    """One spelling per address: no fragment, no trailing slash or punctuation, lower-case."""
    url = url.split("#", 1)[0].rstrip(_TRAILING_PUNCTUATION).rstrip("/")
    return url.lower()


def urls_in(text: str) -> set[str]:
    """Every http(s) URL in `text`, normalized with `normalize_url`."""
    return {normalize_url(m.group(0)) for m in URL.finditer(text)}
