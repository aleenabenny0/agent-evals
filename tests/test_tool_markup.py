from __future__ import annotations

import pytest

from agent_evals.tool_markup import find_tool_markup


@pytest.mark.parametrize(
    "text",
    [
        "A single PUT can upload 5 GB.</answer>",
        '<parameter name="citations">[]',
        '<invoke name="submit_answer">',
        "</function_calls>",
        '<invoke name="submit_answer">',
        "</PARAMETER>",
    ],
)
def test_detects_tool_markup(text: str) -> None:
    assert find_tool_markup(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "A single PUT is limited to 5 GB; 5 GB < 50 TB.",
        "Replace <bucket-name> with your bucket.",
        "Links look like <a href='x'>this</a>.",
        "The answer is in the parameters section.",
        "",
    ],
)
def test_ignores_ordinary_text(text: str) -> None:
    assert find_tool_markup(text) is None
