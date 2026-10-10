"""Detect tool-call markup that leaked into model output.

Claude sometimes writes its native tool-call syntax inside a string field instead of filling
the structured fields. Seen on docs-smoke-003: the answer ended with `</answer>` followed by
`<parameter name="citations">[...]` while the real citations list was empty. The docs agent
uses this check to reject such a submission and the format grader uses the same check to
measure what still slips through, so the definition lives here where both can import it.

The pattern is deliberately narrow. AWS documentation is full of angle brackets that are not
tool markup: placeholders like `<parameter-name>`, XML elements like `<Parameter>`, generics
like `Value<String>`. Claude's syntax is lowercase and its opening `invoke` and `parameter`
tags always carry a `name=` attribute, so the pattern is case-sensitive and requires that
attribute. Whitespace before `>` and an optional namespace prefix such as `antml:` are
tolerated. Changing this pattern changes agent behavior and grading: it is part of the agent
version fingerprint, and `FormatGrader.version` must be bumped with it.
"""

from __future__ import annotations

import re

_NS = r"(?:[a-z]+:)?"  # optional namespace prefix, e.g. `antml:`
_END = r"\s*>"  # closing bracket, tolerating whitespace such as `</answer >`

TOOL_MARKUP = re.compile(
    # Opening tags of the native syntax. The name attribute is what separates them from
    # placeholders and XML elements.
    rf"<{_NS}(?:invoke|parameter)\s+name="
    # Wrapper tags are distinctive on their own, opening or closing.
    rf"|</?{_NS}(?:function_calls|tool_call){_END}"
    # Closing tags of the native syntax, and closing tags named after submit_answer fields.
    rf"|</{_NS}(?:invoke|parameter|answer|citations|abstained|confidence){_END}"
)


def find_tool_markup(text: str) -> str | None:
    """Return the first piece of tool-call markup in `text`, or None if the text is clean."""
    match = TOOL_MARKUP.search(text)
    return match.group(0) if match else None
