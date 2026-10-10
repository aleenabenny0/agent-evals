from __future__ import annotations

from pathlib import Path

import httpx

from agent_evals.agents.docs.snapshot import build_snapshot, html_to_page, read_url_list
from agent_evals.corpus import Corpus, normalize, tokenize
from conftest import BUCKETS_URL, LAMBDA_URL

HTML = """<html><head><title>Bucket naming rules - Amazon S3</title></head>
<body><nav>Site menu that should be dropped</nav>
<div id="main-col-body"><h1>Bucket naming rules</h1>
<p>Bucket names must be between 3 (min) and 63 (max) characters long.</p>
<p>Bucket names can consist only of lowercase letters, numbers, periods, and hyphens.</p>
<p>Bucket names must begin and end with a letter or number, and must be unique within a
partition.</p>
<script>tracking()</script></div><footer>Footer text</footer></body></html>"""


def test_search_ranks_the_right_page_first(corpus: Corpus) -> None:
    assert corpus.search("lambda function timeout")[0].url == LAMBDA_URL
    assert corpus.search("bucket name characters")[0].url == BUCKETS_URL
    assert corpus.search("the of and") == []


def test_read_paginates(corpus: Corpus) -> None:
    first = corpus.read(BUCKETS_URL, 0, 40)
    assert first is not None and "start=40" in first
    assert corpus.read("https://missing.test") is None


def test_normalize_handles_markdown_and_typography() -> None:
    assert normalize("**Bucket**  names—*must*") == normalize("bucket names-must")
    assert normalize("[link text](https://x.test)") == "link text"


def test_fingerprint_is_order_independent(corpus: Corpus) -> None:
    reversed_corpus = Corpus(list(reversed(list(corpus.pages.values()))))
    assert reversed_corpus.fingerprint == corpus.fingerprint


def test_html_to_page_keeps_content_and_drops_chrome() -> None:
    page = html_to_page("https://x.test/p.html", HTML, "2026-10-10")
    assert page.title == "Bucket naming rules - Amazon S3"
    assert "63 (max) characters" in page.text
    assert "Site menu" not in page.text and "tracking" not in page.text
    assert page.text.startswith("# Bucket naming rules")


def test_build_snapshot_records_failures(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok.html":
            return httpx.Response(200, text=HTML)
        return httpx.Response(404, text="not found")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = build_snapshot(
        ["https://x.test/ok.html", "https://x.test/missing.html"],
        tmp_path,
        client=client,
        delay_s=0,
    )
    assert report.pages == 1 and len(report.failures) == 1
    corpus = Corpus.load(tmp_path)
    assert corpus.contains_quote("https://x.test/ok.html", "between 3 (min) and 63 (max)")


def test_read_url_list_skips_comments(tmp_path: Path) -> None:
    f = tmp_path / "pages.txt"
    f.write_text("# header\nhttps://a.test  # trailing\n\nhttps://b.test\n")
    assert read_url_list(f) == ["https://a.test", "https://b.test"]


def test_failed_snapshot_keeps_the_previous_one(tmp_path: Path) -> None:
    (tmp_path / "pages.jsonl").write_text("previous\n")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    report = build_snapshot(["https://x.test/a.html"], tmp_path, client=client, delay_s=0)
    assert report.pages == 0
    assert (tmp_path / "pages.jsonl").read_text() == "previous\n"


def test_non_breaking_spaces_match_and_tokenize(corpus: Corpus) -> None:
    # AWS pages put U+00A0 inside product names ("Amazon S3"). A quote typed with a plain space
    # must still match, and search must see two tokens, not one glued word.
    assert normalize("Bucket\u00a0names") == "bucket names"
    assert tokenize("Bucket\u00a0names") == ["bucket", "names"]
    assert corpus.contains_quote(BUCKETS_URL, "between 3 and\u00a063 characters")
