"""Build a dated, versioned snapshot of AWS documentation pages.

The agent and the graders both read the snapshot, never the live site, so a docs change can't
silently move the score. Refreshing the snapshot is a deliberate, measured change.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel

from agent_evals.corpus import Page
from agent_evals.harness.cache import sha256_text

USER_AGENT = "agent-evals-snapshot/0.1 (research; +https://github.com/aleenabenny0/agent-evals)"

# Tried in order; the first node with enough text wins.
CONTENT_SELECTORS = (
    "#main-col-body",
    "#main-content",
    "main",
    "article",
    "div[role='main']",
    "#awsdocs-content",
    "body",
)
_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form")
_MIN_CONTENT_CHARS = 200


class SnapshotReport(BaseModel):
    created_at: str
    pages: int
    failures: list[dict[str, str]]


def read_url_list(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def html_to_page(url: str, html: str, fetched_at: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else url
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    node = None
    for selector in CONTENT_SELECTORS:
        candidate = soup.select_one(selector)
        if candidate is not None and len(candidate.get_text(strip=True)) >= _MIN_CONTENT_CHARS:
            node = candidate
            break
    if node is None:
        raise ValueError("no content node with enough text")

    text = markdownify(str(node), heading_style="ATX", strip=["img"])
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Page(url=url, title=title, text=text, fetched_at=fetched_at, sha256=sha256_text(text))


def build_snapshot(
    urls: Iterable[str],
    out_dir: Path,
    *,
    client: httpx.Client | None = None,
    delay_s: float = 0.5,
) -> SnapshotReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).isoformat()
    own_client = client is None
    http = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
    )
    pages: list[Page] = []
    failures: list[dict[str, str]] = []
    try:
        for i, url in enumerate(urls):
            if i and delay_s:
                time.sleep(delay_s)  # be polite to the docs site
            try:
                resp = http.get(url)
                resp.raise_for_status()
                pages.append(html_to_page(url, resp.text, fetched_at))
            except (httpx.HTTPError, ValueError) as exc:
                failures.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if own_client:
            http.close()

    report = SnapshotReport(created_at=fetched_at, pages=len(pages), failures=failures)
    if not pages:
        return report  # never replace a good snapshot with an empty one

    with (out_dir / "pages.jsonl").open("w", encoding="utf-8") as fh:
        for page in pages:
            fh.write(page.model_dump_json() + "\n")
    (out_dir / "snapshot.json").write_text(
        json.dumps(report.model_dump(), indent=2), encoding="utf-8"
    )
    return report
