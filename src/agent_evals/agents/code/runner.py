"""Run one job of model-written boto3 code against moto, and nothing else.

This file is the only code in the sandbox image (deploy/sandbox.Dockerfile), so it imports only the
standard library, boto3 and moto. A job arrives as JSON on stdin, and the outcome leaves on the last
line of stdout after a marker, so whatever the job prints cannot be mistaken for it. On the host the
offline tests call `run_job` directly, and only code a test wrote runs that way
(docs/plans/phase-3-code-agent.md, part 3).

A job has a `setup` that creates what already exists, the `code`, which must define
`solve(session)`, and an optional hidden `test`, which sees `session` and solve's `result`. The
outcome names the stage that failed, so a broken setup, which is a defect in the case, is never
scored as the agent's failure.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from collections.abc import Iterator
from typing import Any

MARKER = "@@sandbox-outcome@@"
STDOUT_CHARS = 4000  # what the agent sees of its own prints
TEXT_CHARS = 1000  # an error message or a result's repr
REGION = "us-east-1"
FAKE_CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": REGION,
}


@contextlib.contextmanager
def fake_credentials() -> Iterator[None]:
    """Credentials that cannot reach a real account, restored afterwards."""
    held = {k: os.environ.get(k) for k in FAKE_CREDENTIALS}
    os.environ.update(FAKE_CREDENTIALS)
    try:
        yield
    finally:
        for key, value in held.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    import boto3
    from moto import mock_aws

    printed = io.StringIO()
    stage = "setup"
    result: Any = None
    try:
        with fake_credentials(), mock_aws(), contextlib.redirect_stdout(printed):
            session = boto3.Session(region_name=job.get("region") or REGION)
            exec(compile(job.get("setup") or "", "<setup>", "exec"), {"session": session})
            stage = "code"
            space: dict[str, Any] = {"__name__": "solution"}
            exec(compile(str(job["code"]), "<solution>", "exec"), space)
            solve = space.get("solve")
            if not callable(solve):
                raise TypeError("the code defines no function solve(session)")
            stage = "solve"
            result = solve(session)
            if job.get("test"):
                stage = "test"
                exec(
                    compile(str(job["test"]), "<test>", "exec"),
                    {"session": session, "result": result},
                )
    except BaseException as exc:  # SystemExit too: the job's code must not end the run unseen
        return outcome(False, stage, f"{type(exc).__name__}: {exc}", printed.getvalue(), None)
    return outcome(True, "done", None, printed.getvalue(), result)


def outcome(ok: bool, stage: str, error: str | None, printed: str, result: Any) -> dict[str, Any]:
    return {
        "ok": ok,
        "stage": stage,
        "error": None if error is None else error[:TEXT_CHARS],
        "stdout": printed[:STDOUT_CHARS],
        "result": None if result is None else repr(result)[:TEXT_CHARS],
    }


def main() -> None:
    job = json.loads(sys.stdin.read())
    sys.stdin.close()
    print(MARKER + json.dumps(run_job(job)), flush=True)


if __name__ == "__main__":
    main()
