"""Content-addressed cache for model calls.

Every request is keyed by a hash of its full contents plus a salt. Re-running an unchanged
case returns the identical response for free, which makes results reproducible and CI cheap.
The salt keeps repeated attempts at the same case distinct (see docs/adr/0001).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CallCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(request: dict[str, Any], salt: str = "") -> str:
        return sha256_text(canonical_json({"request": request, "salt": salt}))

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so a crash never leaves a half-written entry.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(value))
        os.replace(tmp, path)
