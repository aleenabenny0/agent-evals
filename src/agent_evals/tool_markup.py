"""Detect tool-call markup that leaked into model output.

Claude sometimes writes its native tool-call syntax inside a string field instead of filling
the structured fields. Seen on docs-smoke-003: the answer ended with `</answer>` followed by
`<parameter name="citations">[...]` while the real citations list was empty. The docs agent
uses this check to reject such a submission and the format grader uses the same check to
measure what still slips through, so the definition lives here where both can import it.
"""

from __future__ import annotations

import re

# Claude's native tool-call tags, optionally namespaced (e.g. `antml:invoke`), plus closing
# tags named after submit_answer fields, which is how the leak looked in practice.
TOOL_MARKUP = re.compile(
    r"</?(?:[a-z]+:)?(?:function_calls|invoke|parameter)\b"
    r"|</(?:answer|citations|abstained|confidence)>",
    re.IGNORECASE,
)


def find_tool_markup(text: str) -> str | None:
    """Return the first piece of tool-call markup in `text`, or None if the text is clean."""
    match = TOOL_MARKUP.search(text)
    return match.group(0) if match else None
