"""The review and label screens must find a quote exactly when the citation grader does."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agent_evals.corpus import Corpus
from agent_evals.review import quote_context, quote_pattern
from conftest import make_page

# The case Aleena hit while labeling: bold markers and a line break inside the cited sentence.
PROV_THROUGHPUT_PAGE = (
    "## Provisioned Throughput\n\n"
    "You can purchase **Provisioned Throughput** to provision a higher level of throughput for a\n"
    "model at a fixed cost.\nIf you customized a model, you must purchase Provisioned Throughput."
)
PROV_THROUGHPUT_QUOTE = (
    "You can purchase Provisioned Throughput to provision a higher level of throughput for a "
    "model at a fixed cost."
)


def test_bold_markers_and_line_breaks_do_not_hide_a_quote() -> None:
    found = quote_context(PROV_THROUGHPUT_PAGE, PROV_THROUGHPUT_QUOTE, width=12)
    assert found is not None
    before, match, after = found
    # The match is the raw page text, markdown and newline included, so it highlights in place.
    assert match == (
        "You can purchase **Provisioned Throughput** to provision a higher level of throughput "
        "for a\nmodel at a fixed cost."
    )
    assert before.endswith("Throughput\n\n") and after.startswith("\nIf you")


RSQUO, EN_DASH = chr(0x2019), chr(0x2013)  # curly apostrophe and en dash, as AWS pages use


def test_table_rows_links_and_typography_match_as_the_grader_sees_them() -> None:
    page = (
        "| Fargate On-Demand vCPU resource count | Each supported Region: 6 | "
        "[Yes](https://console.aws.amazon.com/servicequotas/) | The number of vCPUs |\n"
        f"Bucket names can{RSQUO}t be changed {EN_DASH} ever."
    )
    row_quote = "Fargate On-Demand vCPU resource count | Each supported Region: 6 | Yes | The number"
    row = quote_context(page, row_quote)
    assert row is not None and row[1].startswith("Fargate") and row[1].endswith("The number")
    curly = quote_context(page, "names can't be changed - ever")
    assert curly is not None and curly[1] == f"names can{RSQUO}t be changed {EN_DASH} ever"
    assert quote_context(page, "Each supported Region: 7") is None  # similar words, wrong fact
    assert quote_pattern("   ") is None


def test_quote_context_agrees_with_the_citation_grader(corpus: Corpus) -> None:
    quotes = [
        "between 3 and 63 characters long",  # page has **3** and **63**
        "Function timeout | 900 seconds (15 minutes)",  # table row
        "The name my-example-bucket is valid",  # page has `my-example-bucket` in backticks
        "between 3 and 64 characters",  # not on any page
    ]
    for url, page in corpus.pages.items():
        for quote in quotes:
            grader_says = corpus.contains_quote(url, quote)
            screen_says = quote_context(page.text, quote) is not None
            assert grader_says == screen_says, (url, quote)


@settings(deadline=None, max_examples=60)
@given(st.text(alphabet="ab3 *_`|\n", min_size=1, max_size=20))
def test_a_quote_cut_from_the_page_is_always_found_when_the_grader_finds_it(snippet: str) -> None:
    # Any snippet of a page that survives normalization is a quote the grader accepts; the
    # screen must then locate it too.
    page = "Intro line.\n" + snippet + "\nTrailing **line** here."
    corpus = Corpus([make_page("https://t/p", "t", page)])
    if corpus.contains_quote("https://t/p", snippet):
        assert quote_context(page, snippet) is not None
