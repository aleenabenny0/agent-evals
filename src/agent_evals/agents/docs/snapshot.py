"""Build a dated, versioned snapshot of AWS documentation pages, and detect when it drifts.

The agent and the graders both read the snapshot, never the live site, so a docs change can't
silently move the score. Refreshing the snapshot is a deliberate, measured change.

Drift detection (docs/adr/0005): `evals snapshot` also writes a *lock file* with one hash per
page and no content, which is committed. `evals snapshot --check` fetches the same pages again,
compares hashes with the lock, and lists the suite cases whose evidence pages changed so a
human can re-verify them. A monthly workflow runs the check and opens an issue when it drifts.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel, Field

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


class LockedPage(BaseModel):
    """What the lock remembers about a page: enough to detect a change, none of the content."""

    sha256: str
    title: str
    fetched_at: str


class SnapshotLock(BaseModel):
    """Committed record of the snapshot's page hashes, so drift can be detected anywhere."""

    created_at: str
    pages: dict[str, LockedPage] = Field(default_factory=dict)

    @classmethod
    def from_pages(cls, pages: Iterable[Page], created_at: str) -> SnapshotLock:
        return cls(
            created_at=created_at,
            pages={
                p.url: LockedPage(sha256=p.sha256, title=p.title, fetched_at=p.fetched_at)
                for p in pages
            },
        )

    @classmethod
    def load(cls, path: Path) -> SnapshotLock:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = self.model_copy(update={"pages": dict(sorted(self.pages.items()))})
        path.write_text(ordered.model_dump_json(indent=2) + "\n", encoding="utf-8")


class SnapshotDiff(BaseModel):
    """Live pages compared with the lock. `changed` and `missing` are the ones that matter."""

    lock_created_at: str
    checked_at: str
    unchanged: list[str]
    changed: list[str]  # fetched fine, but the text differs from the lock
    missing: list[str]  # in the lock, could not be fetched now
    new: list[str]  # fetched fine, not in the lock (added to the page list since)
    errors: dict[str, str]  # url -> fetch error

    @property
    def drifted(self) -> bool:
        return bool(self.changed or self.missing)


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


def fetch_pages(
    urls: Iterable[str],
    *,
    client: httpx.Client | None = None,
    delay_s: float = 0.5,
) -> tuple[list[Page], list[dict[str, str]], str]:
    """Fetch every URL once. Returns (pages, failures, fetched_at); one bad page never raises."""
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
    return pages, failures, fetched_at


def build_snapshot(
    urls: Iterable[str],
    out_dir: Path,
    *,
    client: httpx.Client | None = None,
    delay_s: float = 0.5,
    lock_path: Path | None = None,
) -> SnapshotReport:
    """Fetch the pages and replace the snapshot; also write the lock file when a path is given."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages, failures, fetched_at = fetch_pages(urls, client=client, delay_s=delay_s)
    report = SnapshotReport(created_at=fetched_at, pages=len(pages), failures=failures)
    if not pages:
        return report  # never replace a good snapshot with an empty one

    with (out_dir / "pages.jsonl").open("w", encoding="utf-8") as fh:
        for page in pages:
            fh.write(page.model_dump_json() + "\n")
    (out_dir / "snapshot.json").write_text(
        json.dumps(report.model_dump(), indent=2), encoding="utf-8"
    )
    if lock_path is not None:
        SnapshotLock.from_pages(pages, fetched_at).save(lock_path)
    return report


def diff_snapshot(
    pages: Iterable[Page],
    failures: Iterable[Mapping[str, str]],
    lock: SnapshotLock,
    *,
    checked_at: str,
) -> SnapshotDiff:
    """Compare freshly fetched pages with the lock. Pure: no network, no files."""
    by_url = {p.url: p for p in pages}
    unchanged: list[str] = []
    changed: list[str] = []
    new: list[str] = []
    for url, page in sorted(by_url.items()):
        locked = lock.pages.get(url)
        if locked is None:
            new.append(url)
        elif locked.sha256 == page.sha256:
            unchanged.append(url)
        else:
            changed.append(url)
    return SnapshotDiff(
        lock_created_at=lock.created_at,
        checked_at=checked_at,
        unchanged=unchanged,
        changed=changed,
        missing=sorted(url for url in lock.pages if url not in by_url),
        new=new,
        errors={f["url"]: f["error"] for f in failures},
    )


def drift_report(
    diff: SnapshotDiff,
    lock: SnapshotLock,
    affected: Mapping[str, list[tuple[str, str]]],
) -> str:
    """Markdown summary for humans; the monthly workflow posts it as a GitHub issue.

    `affected` maps a changed or missing URL to the (suite file, case id) pairs citing it.
    """
    lines = [
        f"# Docs drift check, {diff.checked_at[:10]}",
        "",
        f"Lock created {diff.lock_created_at[:10]} with {len(lock.pages)} pages. "
        f"Now: {len(diff.unchanged)} unchanged, {len(diff.changed)} changed, "
        f"{len(diff.missing)} missing, {len(diff.new)} new.",
        "",
    ]
    if diff.changed:
        lines += [f"## Changed pages ({len(diff.changed)})", ""]
        lines += [f"- {url} ({lock.pages[url].title})" for url in diff.changed]
        lines.append("")
    if diff.missing:
        lines += [f"## Missing pages ({len(diff.missing)}): fetch failed", ""]
        lines += [f"- {url}: {diff.errors.get(url, 'not fetched')}" for url in diff.missing]
        lines.append("")
    if diff.new:
        lines += [f"## New pages ({len(diff.new)}): in the page list, not in the lock", ""]
        lines += [f"- {url}" for url in diff.new]
        lines.append("")
    if affected:
        total = sum(len(v) for v in affected.values())
        lines += [f"## Cases to re-verify ({total})", ""]
        for url in sorted(affected):
            by_suite: dict[str, list[str]] = {}
            for suite, case_id in affected[url]:
                by_suite.setdefault(suite, []).append(case_id)
            for suite, ids in sorted(by_suite.items()):
                lines.append(f"- {suite}: {', '.join(ids)} ({url})")
        lines.append("")
    elif diff.drifted:
        lines += ["No suite case cites a changed or missing page.", ""]
    if diff.drifted:
        lines += [
            "## What to do",
            "",
            "1. Run `uv run evals snapshot` to refresh the local snapshot and the lock file.",
            "2. Re-read each case listed above against its page; fix the gold answer or quote "
            "only if the documentation changed, never to make the agent pass.",
            "3. Commit `configs/docs_pages.lock.json` with the case changes. Runs on the new "
            "snapshot carry a new corpus fingerprint and are not strictly comparable with "
            "older runs.",
            "",
        ]
    else:
        lines += ["No drift: every locked page is unchanged.", ""]
    return "\n".join(lines)
