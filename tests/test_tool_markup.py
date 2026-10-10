from __future__ import annotations

import pytest

from agent_evals.tool_markup import find_tool_markup

# Labeled fixtures. LEAKED are shapes of tool-call syntax written into a string field; LEGITIMATE
# are angle brackets that appear in real AWS documentation and must never cause a rejection.
LEAKED = [
    "A single PUT can upload 5 GB.</answer>",
    "63.</answer >",
    "63.</answer\n>",
    "</answer>",
    '<parameter name="citations">[]',
    '<parameter\n  name="citations">',
    '<parameter name="citations">',
    "</parameter>",
    '<invoke name="submit_answer">',
    "</invoke>",
    "<function_calls>",
    "</function_calls>",
    "<tool_call>",
    "</tool_call>",
    "</citations>",
    "</abstained>",
    "</confidence>",
]
LEGITIMATE = [
    "aws ssm get-parameter --name <parameter-name>",
    "<Parameter>value</Parameter>",
    "AWS::SSM::Parameter::Value<String>",
    "Parameters:\n  BucketName:\n    Type: String",
    "A single PUT is limited to 5 GB; 5 GB < 50 TB.",
    "Replace <bucket-name> with your bucket.",
    "Links look like <a href='x'>this</a>.",
    "The answer is in the parameters section.",
    "<parameters>",
    "</PARAMETER>",
    "invoke name=submit_answer without a bracket",
    "",
]


@pytest.mark.parametrize("text", LEAKED)
def test_detects_leaked_markup(text: str) -> None:
    assert find_tool_markup(text) is not None


@pytest.mark.parametrize("text", LEGITIMATE)
def test_ignores_legitimate_text(text: str) -> None:
    assert find_tool_markup(text) is None


def test_returns_the_matched_snippet() -> None:
    assert find_tool_markup("fine so far</answer >tail") == "</answer >"
