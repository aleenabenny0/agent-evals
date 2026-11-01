"""The prompt registry: which prompt version serves traffic, changed without a deploy.

Each version's text stays in `agents/docs/prompts.py`, so it is reviewed like code. This module
holds only the live pointers, in one YAML file: the champion, an optional canary with its share of
traffic, and the champion before the last promotion, so a rollback needs no memory of what ran.

Every pointer records the hash of the text it names, and the file is refused when a text no longer
matches, so a prompt cannot change under a version name that is serving. The service rereads the
file when it changes, so a rollback takes effect with no restart. A file that goes bad while
serving leaves the last good state in force and says so, because an outage is a worse answer to a
typo than the previous prompt is. Every change is appended to a log: who, when, why, from and to.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_evals.harness.cache import sha256_text

Role = Literal["champion", "canary"]


class RegistryError(RuntimeError):
    """A registry file that cannot be used, or a change that cannot be made."""


class Pointer(BaseModel):
    """A prompt version and the hash of its text when it was registered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    sha256: str


class RegistryState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    champion: Pointer
    canary: Pointer | None = None
    canary_share: float = Field(default=0.0, ge=0.0, le=1.0)
    previous: Pointer | None = None  # the champion before the last promotion


@dataclass(frozen=True)
class Assignment:
    version: str
    role: Role


def prompt_hash(text: str) -> str:
    return sha256_text(text)[:12]


def assign(state: RegistryState, request_id: str) -> Assignment:
    """Which version serves this request. A hash of its id decides, so a split is deterministic:
    the same id always lands on the same side, and the shares can be checked after a run."""
    if state.canary is not None:
        draw = int(sha256_text(request_id)[:8], 16) / 2**32
        if draw < state.canary_share:
            return Assignment(state.canary.version, "canary")
    return Assignment(state.champion.version, "champion")


class PromptRegistry:
    def __init__(self, path: Path, log_path: Path, prompts: Mapping[str, str]) -> None:
        self.path = path
        self.log_path = log_path
        self.prompts = prompts
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._state = self._read()
        self._stamp = self._file_stamp()
        self.error: str | None = None  # why the file on disk is not the state in force

    def pointer(self, version: str) -> Pointer:
        """A pointer to a version as its text is now; refused for an unknown version."""
        if version not in self.prompts:
            raise RegistryError(f"unknown prompt version {version!r}: {sorted(self.prompts)}")
        return Pointer(version=version, sha256=prompt_hash(self.prompts[version]))

    def state(self) -> RegistryState:
        """The state in force: the file's, reread when it has changed, or the last good one."""
        with self._lock:
            stamp = self._file_stamp()
            if stamp != self._stamp:
                self._stamp = stamp
                try:
                    self._state = self._read()
                    self.error = None
                except RegistryError as exc:
                    self.error = str(exc)
            return self._state

    def set_canary(self, version: str, share: float, *, who: str, why: str) -> RegistryState:
        if not 0.0 < share <= 1.0:
            raise RegistryError(f"a canary's share is above 0 and at most 1, not {share}")

        def change(state: RegistryState) -> RegistryState:
            canary = self.pointer(version)
            if canary == state.champion:
                raise RegistryError(f"{version} is the champion already")
            return state.model_copy(update={"canary": canary, "canary_share": share})

        return self._change("canary", who, why, change)

    def promote(self, *, who: str, why: str) -> RegistryState:
        def change(state: RegistryState) -> RegistryState:
            if state.canary is None:
                raise RegistryError("there is no canary to promote")
            return RegistryState(champion=state.canary, previous=state.champion)

        return self._change("promote", who, why, change)

    def rollback(self, *, who: str, why: str) -> RegistryState:
        """Drop the canary if there is one; otherwise go back to the previous champion."""

        def change(state: RegistryState) -> RegistryState:
            if state.canary is not None:
                return state.model_copy(update={"canary": None, "canary_share": 0.0})
            if state.previous is None:
                raise RegistryError("there is nothing to roll back to")
            return RegistryState(champion=state.previous)

        return self._change("rollback", who, why, change)

    @classmethod
    def create(
        cls, path: Path, log_path: Path, prompts: Mapping[str, str], version: str
    ) -> PromptRegistry:
        """A new registry file with this version as champion. An existing file is left alone."""
        if path.exists():
            raise RegistryError(f"{path} exists")
        if version not in prompts:
            raise RegistryError(f"unknown prompt version {version!r}: {sorted(prompts)}")
        state = RegistryState(
            champion=Pointer(version=version, sha256=prompt_hash(prompts[version]))
        )
        _write_yaml(path, state)
        return cls(path, log_path, prompts)

    # ---- internals ---------------------------------------------------------------------------

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            held = self.path.stat()
        except OSError:
            return None
        return held.st_mtime_ns, held.st_size

    def _read(self) -> RegistryState:
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            state = RegistryState.model_validate(raw)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise RegistryError(f"{self.path} cannot be read: {exc}") from exc
        for pointer in (state.champion, state.canary, state.previous):
            if pointer is not None and self._problem(pointer) is not None:
                raise RegistryError(f"{self.path}: {self._problem(pointer)}")
        return state

    def _problem(self, pointer: Pointer) -> str | None:
        text = self.prompts.get(pointer.version)
        if text is None:
            return f"unknown prompt version {pointer.version!r}"
        if prompt_hash(text) != pointer.sha256:
            return f"the text of {pointer.version} is not the text that was registered"
        return None

    def _change(self, action: str, who: str, why: str, change: Any) -> RegistryState:
        """One change, made from the file as it is now and written whole, then logged."""
        if not why.strip():
            raise RegistryError("a registry change needs a reason")
        with self._lock:
            before = self._read()
            after: RegistryState = change(before)
            _write_yaml(self.path, after)
            self._state, self._stamp, self.error = after, self._file_stamp(), None
            entry = {
                "time": datetime.now(UTC).isoformat(timespec="seconds"),
                "action": action,
                "who": who,
                "why": why,
                "from": before.model_dump(mode="json"),
                "to": after.model_dump(mode="json"),
            }
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            return after


def _write_yaml(path: Path, state: RegistryState) -> None:
    """Written to a temporary file and renamed, so a reader never sees half a registry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(state.model_dump(mode="json"), fh, sort_keys=False)
    os.replace(tmp, path)
