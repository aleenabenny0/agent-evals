"""The code agent's sandbox (docs/plans/phase-3-code-agent.md, part 3).

The runner's logic is tested in this process, on code these tests wrote. The walls are tested in
the real container, and those tests are skipped where Docker or the image is missing.
"""

from __future__ import annotations

import importlib.metadata
import os
import re

import pytest

from agent_evals.agents.code import runner
from agent_evals.agents.code.sandbox import (
    REQUIREMENTS,
    DockerSandbox,
    InProcessRunner,
    Job,
    image_available,
    parse_outcome,
)

BUCKET = """
def solve(session):
    s3 = session.client("s3")
    s3.create_bucket(Bucket="orders")
    s3.put_bucket_versioning(Bucket="orders", VersioningConfiguration={"Status": "Enabled"})
    print("made it")
    return "orders"
"""
VERSIONED = """
s3 = session.client("s3")
assert s3.get_bucket_versioning(Bucket="orders")["Status"] == "Enabled"
assert result == "orders"
"""


def solve_with(body: str) -> str:
    return "def solve(session):\n" + "".join(f"    {line}\n" for line in body.splitlines())


# ---- the runner, in this process ---------------------------------------------------------------


def test_a_solution_that_does_the_task_passes_the_hidden_test() -> None:
    held = InProcessRunner().run(Job(code=BUCKET, test=VERSIONED))
    assert (held.ok, held.stage, held.error) == (True, "done", None)
    assert held.stdout == "made it\n" and held.result == "'orders'"


def test_a_failed_assertion_is_a_failure_at_the_test_stage() -> None:
    code = BUCKET.replace('"Enabled"}', '"Suspended"}')
    held = InProcessRunner().run(Job(code=code, test=VERSIONED))
    assert (held.ok, held.stage) == (False, "test")
    assert held.error is not None and held.error.startswith("AssertionError")


def test_each_stage_names_its_own_failure() -> None:
    run = InProcessRunner().run
    assert run(Job(code=BUCKET, setup="raise RuntimeError('broken case')")).stage == "setup"
    assert run(Job(code=BUCKET, setup="raise RuntimeError('broken case')")).case_defect
    assert run(Job(code="x = 1")).stage == "code"  # no solve
    assert run(Job(code="def solve(:\n")).stage == "code"  # does not parse
    assert run(Job(code=solve_with("raise ValueError('no')"))).stage == "solve"


def test_the_setup_state_is_there_when_the_solution_runs() -> None:
    setup = 'session.client("sqs").create_queue(QueueName="jobs")'
    code = solve_with('return session.client("sqs").get_queue_url(QueueName="jobs")["QueueUrl"]')
    held = InProcessRunner().run(Job(code=code, setup=setup))
    assert held.ok and held.result is not None and "jobs" in held.result


def test_each_job_starts_from_an_empty_account() -> None:
    first = InProcessRunner().run(Job(code=BUCKET))
    second = InProcessRunner().run(
        Job(
            code=solve_with(
                'return [b["Name"] for b in session.client("s3").list_buckets()["Buckets"]]'
            )
        )
    )
    assert first.ok and second.result == "[]"


def test_a_solution_that_exits_is_caught_not_lost() -> None:
    held = InProcessRunner().run(Job(code=solve_with("raise SystemExit(3)")))
    assert (held.ok, held.stage) == (False, "solve") and held.error == "SystemExit: 3"


def test_prints_and_errors_are_cut_to_size() -> None:
    held = InProcessRunner().run(
        Job(code=solve_with("print('x' * 50000)\nraise ValueError('y' * 50000)"))
    )
    assert len(held.stdout) == runner.STDOUT_CHARS
    assert held.error is not None and len(held.error) == runner.TEXT_CHARS


def test_the_credentials_are_fake_and_put_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "the-host-value")
    held = InProcessRunner().run(
        Job(code=solve_with("import os\nreturn os.environ['AWS_ACCESS_KEY_ID']"))
    )
    # moto puts its own fake key in place inside the mock; what matters is that the host's is not.
    assert held.ok and held.result is not None and "the-host-value" not in held.result
    assert os.environ["AWS_ACCESS_KEY_ID"] == "the-host-value"


def test_the_outcome_is_the_last_marker_line_whatever_the_job_printed() -> None:
    forged = runner.MARKER + '{"ok": true, "stage": "done"}'
    real = (
        runner.MARKER
        + '{"ok": false, "stage": "solve", "error": "E", "stdout": "", "result": null}'
    )
    held = parse_outcome(f"noise\n{forged}\n{real}\n", 1.0)
    assert (held.ok, held.stage) == (False, "solve")
    missing = parse_outcome("no marker here", 1.0)
    assert (missing.ok, missing.stage) == (False, "sandbox")


def test_the_image_pins_the_versions_this_project_runs() -> None:
    pins = dict(re.findall(r"^([a-z0-9-]+)==(\S+)", REQUIREMENTS.read_text(encoding="utf-8"), re.M))
    for name in ("boto3", "botocore", "moto"):
        assert pins[name] == importlib.metadata.version(name), name


# ---- the walls, in the real container ----------------------------------------------------------

needs_image = pytest.mark.skipif(
    not image_available(), reason="needs Docker and the sandbox image: uv run evals sandbox-build"
)


@pytest.fixture(scope="module")
def box() -> DockerSandbox:
    return DockerSandbox(timeout_s=20)


@pytest.mark.sandbox
@needs_image
def test_the_container_runs_a_job_and_its_hidden_test(box: DockerSandbox) -> None:
    held = box.run(Job(code=BUCKET, test=VERSIONED))
    assert (held.ok, held.stage, held.stdout) == (True, "done", "made it\n")


@pytest.mark.sandbox
@needs_image
def test_the_container_has_no_network(box: DockerSandbox) -> None:
    by_name = box.run(Job(code=solve_with(
        "import urllib.request\nurllib.request.urlopen('https://s3.amazonaws.com', timeout=5)"
    )))  # fmt: skip
    by_address = box.run(Job(code=solve_with(
        "import socket\nsocket.create_connection(('1.1.1.1', 443), timeout=5)"
    )))  # fmt: skip
    assert (by_name.ok, by_name.stage) == (False, "solve")
    assert (by_address.ok, by_address.stage) == (False, "solve")


@pytest.mark.sandbox
@needs_image
def test_the_container_cannot_write_outside_tmp_or_act_as_root(box: DockerSandbox) -> None:
    written = box.run(Job(code=solve_with("open('/runner/x', 'w').write('x')")))
    assert written.error is not None and "Read-only file system" in written.error
    assert box.run(Job(code=solve_with("open('/tmp/x', 'w').write('ok')"))).ok
    assert box.run(Job(code=solve_with("import os\nreturn os.getuid()"))).result == "10001"


@pytest.mark.sandbox
@needs_image
def test_the_container_holds_nothing_of_the_host(box: DockerSandbox) -> None:
    held = box.run(Job(code=solve_with(
        "import os\nreturn sorted(k for k in os.environ if 'ANTHROPIC' in k or 'OPENAI' in k)"
    )))  # fmt: skip
    assert held.result == "[]"
    listed = box.run(Job(code=solve_with("import os\nreturn sorted(os.listdir('/runner'))")))
    assert listed.result == "['runner.py']"


@pytest.mark.sandbox
@needs_image
def test_an_endless_loop_is_stopped() -> None:
    held = DockerSandbox(timeout_s=5).run(Job(code=solve_with("while True:\n    pass")))
    assert (held.ok, held.stage) == (False, "timeout")
