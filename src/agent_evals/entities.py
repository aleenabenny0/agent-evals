"""Numbers and URLs in text: the entities a deterministic grounding check can compare.

Shared by the grounding grader (is every figure in the answer on a page the agent saw?), the
suite validator (does a support quote cover every figure in the gold answer?) and the review
screen (where on the page do the answer's figures sit?). One extraction, so the three agree.
"""

from __future__ import annotations

import re

# A standalone number: digits with optional thousands separators and a decimal part. It may not
# touch a letter, digit or dot on either side, so "S3", "EC2", "python3.10" and "10.0.0.0" do not
# shed a "3", "2", "3.10" or "0"; "/16", "10,240", "99.9%" and a "63." ending a sentence count.
NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?!\w)(?!\.\d)")
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_TRAILING_PUNCTUATION = ".,;:!?"


def numbers_in(text: str) -> set[str]:
    """Every standalone number in `text`, thousands separators removed ("10,240" -> "10240")."""
    return {m.group(0).replace(",", "") for m in NUMBER.finditer(text)}


def normalize_url(url: str) -> str:
    """One spelling per address: no fragment, no trailing slash or punctuation, lower-case."""
    url = url.split("#", 1)[0].rstrip(_TRAILING_PUNCTUATION).rstrip("/")
    return url.lower()


def urls_in(text: str) -> set[str]:
    """Every http(s) URL in `text`, normalized with `normalize_url`."""
    return {normalize_url(m.group(0)) for m in URL.finditer(text)}
