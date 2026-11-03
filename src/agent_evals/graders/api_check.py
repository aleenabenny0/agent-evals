"""Check boto3 calls in code against botocore's service models, without running the code.

The code is parsed, never imported. A client is a name bound to a `.client("service")` call, and a
call on it is matched to the service's operation by its method name. Keyword arguments are checked
against the operation's input shape, and a literal dict or list is followed into the nested shapes
it fills. Paginators and waiters are followed the same way. What cannot be resolved, such as
`**kwargs`, `getattr`, or a call on a resource, is counted as unchecked and never passed silently
(docs/plans/phase-3-code-agent.md, part 2).

The models are botocore's installed ones, pinned in uv.lock, so an operation newer than the pin
would read as hallucinated. Calls on a client passed into another function are not seen.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import botocore.session
from botocore import xform_name
from botocore.exceptions import UnknownServiceError
from botocore.model import ListShape, MapShape, OperationModel, Shape, StructureShape

# Real client methods that are not operations of the service.
CLIENT_HELPERS = frozenset(
    {
        "can_paginate",
        "close",
        "copy",
        "download_file",
        "download_fileobj",
        "exceptions",
        "generate_presigned_post",
        "generate_presigned_url",
        "get_paginator",
        "get_waiter",
        "meta",
        "upload_file",
        "upload_fileobj",
        "waiter_names",
    }
)
DESTRUCTIVE_PREFIXES = ("Delete", "Terminate", "Remove")


@cache
def _session() -> Any:
    return botocore.session.get_session()


@cache
def _operations(service: str) -> dict[str, str] | None:
    """Method name to operation name for a service, or None if there is no such service."""
    try:
        model = _session().get_service_model(service)
    except UnknownServiceError:
        return None
    return {xform_name(op): op for op in model.operation_names}


def _operation(service: str, name: str) -> OperationModel:
    model: OperationModel = _session().get_service_model(service).operation_model(name)
    return model


@dataclass(frozen=True)
class Call:
    service: str
    method: str
    operation: str | None  # None when the method names no operation
    line: int


@dataclass
class ApiReport:
    calls: list[Call] = field(default_factory=list)
    hallucinated_operations: list[str] = field(default_factory=list)
    hallucinated_parameters: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    syntax_error: str | None = None

    @property
    def operations(self) -> list[str]:
        return [c.operation for c in self.calls if c.operation is not None]

    def destructive(self) -> list[str]:
        return [op for op in self.operations if op.startswith(DESTRUCTIVE_PREFIXES)]


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _service_of(call: ast.Call) -> str | None:
    """`x.client("s3")` or `x.client(service_name="s3")` gives "s3"."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "client"):
        return None
    if call.args:
        return _string(call.args[0])
    for kw in call.keywords:
        if kw.arg == "service_name":
            return _string(kw.value)
    return None


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.report = ApiReport()
        self.clients: dict[str, str] = {}  # name -> service
        self.paginators: dict[str, tuple[str, str]] = {}  # name -> (service, operation)
        self.waiters: dict[str, tuple[str, str]] = {}  # name -> (service, operation)

    # -- bindings ------------------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        value = node.value
        target = node.targets[0] if len(node.targets) == 1 else None
        name = target.id if isinstance(target, ast.Name) else None
        if isinstance(value, ast.Call) and self._is_helper(value):
            for arg in value.args:  # the helper call itself is checked once, here
                self.visit(arg)
            bound = self._helper_target(value)
            if bound is not None and name is not None:
                kind, held = bound
                (self.paginators if kind == "paginator" else self.waiters)[name] = held
            return
        self.generic_visit(node)
        if name is not None and isinstance(value, ast.Call):
            service = _service_of(value)
            if service is not None:
                self.clients[name] = service

    def _is_helper(self, call: ast.Call) -> bool:
        func = call.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in ("get_paginator", "get_waiter")
            and self._client_service(func.value) is not None
        )

    def _helper_target(self, call: ast.Call) -> tuple[str, tuple[str, str]] | None:
        """`client.get_paginator("op")` or `client.get_waiter("name")`, resolved to an operation."""
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("get_paginator", "get_waiter"):
            return None
        service = self._client_service(func.value)
        name = _string(call.args[0]) if call.args else None
        if service is None or name is None:
            return None
        ops = _operations(service) or {}
        if func.attr == "get_paginator":
            operation = ops.get(name)
            try:
                if operation is None:
                    raise ValueError(name)
                _session().get_paginator_model(service).get_paginator(operation)
            except Exception:
                self.report.hallucinated_operations.append(f"{service}.get_paginator({name!r})")
                return None
            return "paginator", (service, operation)
        try:
            waiters = _session().get_waiter_model(service)
            by_method = {xform_name(w): w for w in waiters.waiter_names}
            operation = waiters.get_waiter(by_method[name]).operation
        except Exception:
            self.report.hallucinated_operations.append(f"{service}.get_waiter({name!r})")
            return None
        return "waiter", (service, operation)

    def _client_service(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.clients.get(node.id)
        if isinstance(node, ast.Call):
            return _service_of(node)
        return None

    # -- calls -------------------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and node.args:
            if self._client_service(node.args[0]) is not None:
                self.report.unchecked.append(f"line {node.lineno}: getattr on a client")
            return
        if not isinstance(func, ast.Attribute):
            return
        if isinstance(func.value, ast.Name) and func.attr == "resource":
            self.report.unchecked.append(f"line {node.lineno}: a boto3 resource")
            return
        receiver = func.value
        if (
            isinstance(receiver, ast.Name)
            and receiver.id in self.paginators
            and func.attr == "paginate"
        ):
            self._arguments(node, *self.paginators[receiver.id], extra={"PaginationConfig"})
            return
        if isinstance(receiver, ast.Name) and receiver.id in self.waiters and func.attr == "wait":
            held = self.waiters[receiver.id]
            self._arguments(node, *held, extra={"WaiterConfig"}, required=False)
            return
        service = self._client_service(receiver)
        if service is None:
            return
        if func.attr in CLIENT_HELPERS:
            if func.attr in ("get_paginator", "get_waiter"):
                self._helper_target(node)  # checks the name even when the result is not kept
            return
        ops = _operations(service)
        if ops is None:
            self.report.hallucinated_operations.append(f"{service}: no such service")
            return
        operation = ops.get(func.attr)
        self.report.calls.append(Call(service, func.attr, operation, node.lineno))
        if operation is None:
            self.report.hallucinated_operations.append(f"{service}.{func.attr}")
            return
        self._arguments(node, service, operation)

    def _arguments(
        self,
        node: ast.Call,
        service: str,
        operation: str,
        *,
        extra: set[str] | None = None,
        required: bool = True,
    ) -> None:
        where = f"{service}.{xform_name(operation)}"
        if node.args:
            self.report.hallucinated_parameters.append(f"{where}: positional arguments")
        if any(kw.arg is None for kw in node.keywords):
            self.report.unchecked.append(f"line {node.lineno}: {where} with **kwargs")
            return
        shape = _operation(service, operation).input_shape
        given = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        allowed = set(shape.members) if shape is not None else set()
        for name, value in given.items():
            if extra and name in extra:
                continue
            if name not in allowed:
                self.report.hallucinated_parameters.append(f"{where}: {name}")
            elif shape is not None:
                self._nested(value, shape.members[name], f"{where}: {name}")
        if required and shape is not None:
            for name in shape.required_members:
                if name not in given:
                    self.report.missing_required.append(f"{where}: {name}")

    def _nested(self, value: ast.AST, shape: Shape, where: str) -> None:
        """Follow a literal into the shape it fills. Anything not literal is left alone."""
        if isinstance(shape, StructureShape) and isinstance(value, ast.Dict):
            if any(k is None for k in value.keys):  # {**other}: the keys are not all here
                return
            keys = {_string(k): v for k, v in zip(value.keys, value.values, strict=True)}
            for key, inner in keys.items():
                if key is None:
                    continue
                if key not in shape.members:
                    self.report.hallucinated_parameters.append(f"{where}.{key}")
                else:
                    self._nested(inner, shape.members[key], f"{where}.{key}")
            for key in shape.required_members:
                if key not in keys:
                    self.report.missing_required.append(f"{where}.{key}")
        elif isinstance(shape, ListShape) and isinstance(value, ast.List | ast.Tuple):
            for i, item in enumerate(value.elts):
                self._nested(item, shape.member, f"{where}[{i}]")
        elif isinstance(shape, MapShape) and isinstance(value, ast.Dict):
            for inner in value.values:
                self._nested(inner, shape.value, f"{where}[*]")


def check_api(code: str) -> ApiReport:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ApiReport(syntax_error=f"line {exc.lineno}: {exc.msg}")
    checker = _Checker()
    checker.visit(tree)
    return checker.report
