"""The service's daily spend cap, kept in a file so that a restart cannot reset it."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path


def _today() -> date:
    return datetime.now(UTC).date()


class DailyBudget:
    """What the service spent today, in UTC days. A check comes before an answer, and the cost is
    known only after it, so the last answers of a day can pass the cap by what is in flight."""

    def __init__(self, path: Path, cap_usd: float, *, today: Callable[[], date] = _today) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self.today = today
        self._lock = threading.Lock()

    def spent_usd(self) -> float:
        with self._lock:
            return self._read()

    def exhausted(self) -> bool:
        return self.spent_usd() >= self.cap_usd

    def add(self, usd: float) -> None:
        with self._lock:
            total = self._read() + usd
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"date": self.today().isoformat(), "spent_usd": round(total, 6)}, fh)
            os.replace(tmp, self.path)

    def _read(self) -> float:
        if not self.path.is_file():
            return 0.0
        held = json.loads(self.path.read_text(encoding="utf-8"))
        return float(held["spent_usd"]) if held.get("date") == self.today().isoformat() else 0.0
