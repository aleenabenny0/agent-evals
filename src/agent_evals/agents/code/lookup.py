"""The code agent's lookups: botocore's service models, rendered for a model to read.

They read the same definitions boto3 uses and the API check grades against, from the botocore
pinned in uv.lock, so a lookup is exact and free. A name that matches nothing comes back with the
closest names, so a near miss can be corrected rather than guessed again.
"""

from __future__ import annotations

import difflib
import html
import re

from botocore import xform_name
from botocore.exceptions import UnknownServiceError
from botocore.model import ListShape, MapShape, Shape, StructureShape

from agent_evals.graders.api_check import _operations, _session

MAX_DEPTH = 4
_TAG = re.compile(r"<[^>]+>")


class LookupError_(ValueError):
    """A name that matches nothing; the message holds the closest names."""


def _first_sentence(doc: str | None, limit: int = 300) -> str:
    text = " ".join(html.unescape(_TAG.sub(" ", doc or "")).split())
    end = text.find(". ")
    return (text if end < 0 else text[: end + 1])[:limit]


def _service_operations(service: str) -> dict[str, str]:
    ops = _operations(service)
    if ops is None:
        names = _session().get_available_services()
        close = difflib.get_close_matches(service, names, n=5, cutoff=0.4)
        raise LookupError_(f"No service {service!r}. Closest: {', '.join(close) or 'none'}.")
    return ops


def list_operations(service: str) -> str:
    ops = _service_operations(service)
    return f"{service}: {len(ops)} operations\n" + ", ".join(sorted(ops))


def _shape_line(shape: Shape) -> str:
    kind = str(shape.type_name)
    raw = getattr(shape, "enum", None)
    enum = [str(v) for v in raw] if isinstance(raw, list) else []
    if isinstance(shape, ListShape):
        kind = f"list of {shape.member.type_name}"
    elif isinstance(shape, MapShape):
        kind = f"map of {shape.key.type_name} to {shape.value.type_name}"
    return kind + (f", one of {' | '.join(enum[:12])}" if enum else "")


def _render(shape: Shape, indent: int, depth: int, path: tuple[str, ...], out: list[str]) -> None:
    target: Shape | None = shape
    if isinstance(shape, ListShape):
        target = shape.member
    elif isinstance(shape, MapShape):
        target = shape.value
    if not isinstance(target, StructureShape) or depth >= MAX_DEPTH or target.name in path:
        return
    required = set(target.required_members)
    for name, member in target.members.items():
        mark = "*" if name in required else " "
        out.append(f"{'  ' * indent}{mark} {name}: {_shape_line(member)}")
        _render(member, indent + 1, depth + 1, (*path, target.name), out)


def describe_operation(service: str, operation: str, limit: int = 6000) -> str:
    ops = _service_operations(service)
    name = ops.get(operation) or (operation if operation in ops.values() else None)
    if name is None:
        close = difflib.get_close_matches(xform_name(operation), list(ops), n=5)
        raise LookupError_(
            f"{service} has no operation {operation!r}. Closest: {', '.join(close) or 'none'}."
        )
    model = _session().get_service_model(service).operation_model(name)
    lines = [f"{service}.{xform_name(name)} ({name})", _first_sentence(model.documentation)]
    lines.append("Parameters (* = required):")
    if model.input_shape is None or not model.input_shape.members:
        lines.append("  none")
    else:
        _render(model.input_shape, 1, 0, (), lines)
    if model.output_shape is not None and model.output_shape.members:
        lines.append("Returns: " + ", ".join(model.output_shape.members))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n[cut: describe a narrower part]"


__all__ = ["LookupError_", "UnknownServiceError", "describe_operation", "list_operations"]
