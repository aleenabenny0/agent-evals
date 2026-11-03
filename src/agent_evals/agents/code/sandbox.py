"""Where the code agent's code runs: a Docker container with no network (phase 3, part 3).

Each job gets a fresh container from the sandbox image, with no network, a read-only filesystem
apart from a small /tmp, limits on memory, CPU and processes, no capabilities, and a user with no
rights. Model-written code never runs on the host. The image's tag is a hash of everything built
into it, so a changed runner or a changed pin can never run in a stale image.

`InProcessRunner` runs a job in this process instead. It exists for the offline tests, whose code
a test wrote; nothing that runs model output may use it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_evals.agents.code import runner

ROOT = Path(__file__).resolve().parents[4]
DOCKERFILE = ROOT / "deploy" / "sandbox.Dockerfile"
REQUIREMENTS = ROOT / "deploy" / "sandbox-requirements.txt"
RUNNER = Path(runner.__file__)
IMAGE = "agent-evals-sandbox"
TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Job:
    code: str
    setup: str = ""
    test: str | None = None  # the hidden test; never present when the agent runs its own code

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "setup": self.setup, "test": self.test}


@dataclass(frozen=True)
class RunResult:
    ok: bool
    stage: str  # setup, code, solve or test when it failed; done; or timeout or sandbox
    error: str | None
    stdout: str
    result: str | None
    seconds: float

    @property
    def case_defect(self) -> bool:
        """The setup failed: the case is broken, not the code under test."""
        return not self.ok and self.stage == "setup"


class CodeRunner(Protocol):
    def run(self, job: Job) -> RunResult: ...


def _result(data: dict[str, Any], seconds: float) -> RunResult:
    return RunResult(
        ok=bool(data["ok"]),
        stage=str(data["stage"]),
        error=data.get("error"),
        stdout=str(data.get("stdout") or ""),
        result=data.get("result"),
        seconds=round(seconds, 3),
    )


def parse_outcome(stdout: str, seconds: float) -> RunResult:
    """The runner's outcome: the last line that starts with its marker."""
    lines = [line for line in stdout.splitlines() if line.startswith(runner.MARKER)]
    if not lines:
        return RunResult(False, "sandbox", "the run ended without an outcome", "", None, seconds)
    return _result(json.loads(lines[-1][len(runner.MARKER) :]), seconds)


def image_tag() -> str:
    digest = hashlib.sha256()
    for path in (DOCKERFILE, REQUIREMENTS, RUNNER):
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return f"{IMAGE}:{digest.hexdigest()[:12]}"


def image_available(tag: str | None = None) -> bool:
    try:
        done = subprocess.run(
            ["docker", "image", "inspect", tag or image_tag()], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def build_image(tag: str | None = None) -> str:
    """Build the sandbox image from the repository root. Needs Docker and the network, once."""
    held = tag or image_tag()
    subprocess.run(["docker", "build", "-f", str(DOCKERFILE), "-t", held, str(ROOT)], check=True)
    return held


class DockerSandbox:
    def __init__(self, tag: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        self.tag = tag or image_tag()
        self.timeout_s = timeout_s

    def command(self, name: str) -> list[str]:
        return [
            "docker", "run", "--rm", "-i", "--name", name,
            "--network", "none",
            "--read-only", "--tmpfs", "/tmp:rw,size=16m",
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            self.tag,
        ]  # fmt: skip

    def run(self, job: Job) -> RunResult:
        name = f"agent-evals-sandbox-{uuid.uuid4().hex[:12]}"
        started = time.perf_counter()
        try:
            done = subprocess.run(
                self.command(name),
                input=json.dumps(job.payload()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
            seconds = round(time.perf_counter() - started, 3)
            stopped = f"stopped after {self.timeout_s:g} s"
            return RunResult(False, "timeout", stopped, "", None, seconds)
        return parse_outcome(done.stdout, time.perf_counter() - started)


class InProcessRunner:
    """For the offline tests only: runs a job in this process, with no isolation."""

    def run(self, job: Job) -> RunResult:
        started = time.perf_counter()
        data = runner.run_job(job.payload())
        return _result(data, time.perf_counter() - started)
