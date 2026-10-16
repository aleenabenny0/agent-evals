"""DuckDB results store. One file, queryable with SQL, easy to snapshot."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from agent_evals.types import AgentResult, Grade

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        VARCHAR PRIMARY KEY,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    status        VARCHAR,
    agent         VARCHAR,
    agent_version VARCHAR,
    suite         VARCHAR,
    attempts      INTEGER,
    manifest      VARCHAR
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id        VARCHAR,
    case_id       VARCHAR,
    attempt       INTEGER,
    output        VARCHAR,
    trajectory    VARCHAR,
    input_tokens  BIGINT,
    output_tokens BIGINT,
    model_calls   INTEGER,
    cached_calls  INTEGER,
    cost_usd      DOUBLE,
    latency_ms    DOUBLE,
    error         VARCHAR,
    PRIMARY KEY (run_id, case_id, attempt)
);
CREATE TABLE IF NOT EXISTS grades (
    run_id   VARCHAR,
    case_id  VARCHAR,
    attempt  INTEGER,
    grader   VARCHAR,
    metric   VARCHAR,
    value    DOUBLE,
    passed   BOOLEAN,
    reason   VARCHAR
);
CREATE TABLE IF NOT EXISTS labels (
    run_id      VARCHAR,
    case_id     VARCHAR,
    attempt     INTEGER,
    labeler     VARCHAR,
    supported   BOOLEAN,
    responsive  BOOLEAN,
    note        VARCHAR,
    label_split VARCHAR,
    labeled_at  TIMESTAMP,
    PRIMARY KEY (run_id, case_id, attempt, labeler)
);
-- Added 2026-10-12 with the two-question support rubric; NULL on labels made before it.
ALTER TABLE labels ADD COLUMN IF NOT EXISTS core_supported BOOLEAN;
"""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ResultsStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    # ---- writes -------------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        *,
        agent: str,
        agent_version: str,
        suite: str,
        attempts: int,
        manifest: dict[str, Any],
    ) -> None:
        self.con.execute(
            "INSERT INTO runs VALUES (?, ?, NULL, 'running', ?, ?, ?, ?, ?)",
            [run_id, _now(), agent, agent_version, suite, attempts, json.dumps(manifest)],
        )

    def finish_run(self, run_id: str, status: str) -> None:
        self.con.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
            [_now(), status, run_id],
        )

    def record_attempt(
        self,
        run_id: str,
        case_id: str,
        attempt: int,
        result: AgentResult,
        cost_usd: float | None,
        grades: list[tuple[str, Grade]],
    ) -> None:
        u = result.usage
        self.con.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                case_id,
                attempt,
                json.dumps(result.output),
                json.dumps([s.model_dump(mode="json") for s in result.trajectory]),
                u.input_tokens,
                u.output_tokens,
                u.model_calls,
                u.cached_calls,
                cost_usd,
                u.latency_ms,
                result.error,
            ],
        )
        for grader_name, g in grades:
            self.con.execute(
                "INSERT INTO grades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, case_id, attempt, grader_name, g.metric, g.value, g.passed, g.reason],
            )

    def add_grades(
        self, run_id: str, case_id: str, attempt: int, grades: list[tuple[str, Grade]]
    ) -> None:
        """Grades added after the run, e.g. by the LLM judge."""
        for grader_name, g in grades:
            self.con.execute(
                "INSERT INTO grades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, case_id, attempt, grader_name, g.metric, g.value, g.passed, g.reason],
            )

    def delete_grades(
        self, run_id: str, grader: str, keys: list[tuple[str, int]] | None = None
    ) -> None:
        """Drop one grader's grades for a run, or only for the (case_id, attempt) keys given."""
        if keys is None:
            self.con.execute("DELETE FROM grades WHERE run_id = ? AND grader = ?", [run_id, grader])
            return
        for case_id, attempt in keys:
            self.con.execute(
                "DELETE FROM grades WHERE run_id = ? AND grader = ? AND case_id = ? "
                "AND attempt = ?",
                [run_id, grader, case_id, attempt],
            )

    def annotate_run(self, run_id: str, key: str, value: Any) -> None:
        """Add one entry to a run's manifest, e.g. which judge version graded it."""
        manifest = self.run_info(run_id)["manifest"]
        manifest[key] = value
        self.con.execute(
            "UPDATE runs SET manifest = ? WHERE run_id = ?", [json.dumps(manifest), run_id]
        )

    def record_label(
        self,
        run_id: str,
        case_id: str,
        attempt: int,
        labeler: str,
        *,
        supported: bool | None,
        responsive: bool,
        note: str,
        label_split: str,
        core_supported: bool | None = None,
    ) -> None:
        """A hand label for one stored answer; relabeling by the same person replaces it.

        `supported` is the strict question (every claim quoted); `core_supported` the lenient one
        (the claims that answer the question are quoted). Both None when there were no citations.
        """
        self.con.execute(
            "INSERT OR REPLACE INTO labels (run_id, case_id, attempt, labeler, supported, "
            "responsive, note, label_split, labeled_at, core_supported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                case_id,
                attempt,
                labeler,
                supported,
                responsive,
                note,
                label_split,
                _now(),
                core_supported,
            ],
        )

    # ---- reads --------------------------------------------------------------------

    def answered_attempts(self, run_id: str) -> list[dict[str, Any]]:
        """Attempts that produced an answer: no error and not abstained."""
        rows = self.con.execute(
            "SELECT case_id, attempt, output FROM attempts WHERE run_id = ? AND error IS NULL "
            "ORDER BY case_id, attempt",
            [run_id],
        ).fetchall()
        out = []
        for case_id, attempt, output in rows:
            data = json.loads(output)
            if not data.get("abstained", False):
                out.append({"case_id": str(case_id), "attempt": int(attempt), "output": data})
        return out

    def grade_count(self, run_id: str, grader: str) -> int:
        row = self.con.execute(
            "SELECT COUNT(*) FROM grades WHERE run_id = ? AND grader = ?", [run_id, grader]
        ).fetchone()
        return int(row[0]) if row else 0

    def grades_for(self, run_id: str, grader: str) -> dict[tuple[str, int], dict[str, float]]:
        """(case_id, attempt) -> metric -> value, for one grader."""
        rows = self.con.execute(
            "SELECT case_id, attempt, metric, value FROM grades WHERE run_id = ? AND grader = ?",
            [run_id, grader],
        ).fetchall()
        out: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
        for case_id, attempt, metric, value in rows:
            out[(str(case_id), int(attempt))][str(metric)] = float(value)
        return dict(out)

    def grade_reasons(
        self, run_id: str, grader: str
    ) -> dict[tuple[str, int], dict[str, tuple[float, str]]]:
        """(case_id, attempt) -> metric -> (value, reason), for one grader."""
        rows = self.con.execute(
            "SELECT case_id, attempt, metric, value, reason FROM grades "
            "WHERE run_id = ? AND grader = ?",
            [run_id, grader],
        ).fetchall()
        out: dict[tuple[str, int], dict[str, tuple[float, str]]] = defaultdict(dict)
        for case_id, attempt, metric, value, reason in rows:
            out[(str(case_id), int(attempt))][str(metric)] = (float(value), str(reason or ""))
        return dict(out)

    def labels(self, run_id: str, labeler: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT case_id, attempt, labeler, supported, responsive, note, label_split, "
            "core_supported FROM labels WHERE run_id = ?"
        )
        params: list[Any] = [run_id]
        if labeler is not None:
            sql += " AND labeler = ?"
            params.append(labeler)
        rows = self.con.execute(sql + " ORDER BY case_id, attempt", params).fetchall()
        keys = (
            "case_id",
            "attempt",
            "labeler",
            "supported",
            "responsive",
            "note",
            "label_split",
            "core_supported",
        )
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def latest_run_id(self) -> str | None:
        row = self.con.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def run_info(self, run_id: str) -> dict[str, Any]:
        row = self.con.execute(
            "SELECT run_id, started_at, finished_at, status, agent, agent_version, suite, "
            "attempts, manifest FROM runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        keys = [
            "run_id",
            "started_at",
            "finished_at",
            "status",
            "agent",
            "agent_version",
            "suite",
            "attempts",
            "manifest",
        ]
        info = dict(zip(keys, row, strict=True))
        info["manifest"] = json.loads(info["manifest"])
        return info

    def metrics(self, run_id: str) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT metric FROM grades WHERE run_id = ? ORDER BY metric", [run_id]
        ).fetchall()
        return [str(r[0]) for r in rows]

    def metric_by_case(self, run_id: str, metric: str) -> dict[str, list[float]]:
        """case_id -> metric value for each attempt, in attempt order."""
        rows = self.con.execute(
            "SELECT case_id, attempt, value FROM grades WHERE run_id = ? AND metric = ? "
            "ORDER BY case_id, attempt",
            [run_id, metric],
        ).fetchall()
        out: dict[str, list[float]] = defaultdict(list)
        for case_id, _attempt, value in rows:
            out[str(case_id)].append(float(value))
        return dict(out)

    def failures(self, run_id: str, metric: str = "correct") -> list[tuple[str, int, str]]:
        rows = self.con.execute(
            "SELECT case_id, attempt, reason FROM grades "
            "WHERE run_id = ? AND metric = ? AND passed = FALSE ORDER BY case_id, attempt",
            [run_id, metric],
        ).fetchall()
        return [(str(c), int(a), str(r)) for c, a, r in rows]

    def latencies(self, run_id: str) -> list[float]:
        rows = self.con.execute(
            "SELECT latency_ms FROM attempts WHERE run_id = ? AND error IS NULL", [run_id]
        ).fetchall()
        return [float(r[0]) for r in rows]

    def all_attempts(self, run_id: str) -> list[dict[str, Any]]:
        """Every stored attempt of a run with what a grader needs: output, trajectory, error."""
        rows = self.con.execute(
            "SELECT case_id, attempt, output, trajectory, error FROM attempts WHERE run_id = ? "
            "ORDER BY case_id, attempt",
            [run_id],
        ).fetchall()
        return [
            {
                "case_id": str(c),
                "attempt": int(a),
                "output": json.loads(o),
                "trajectory": json.loads(t),
                "error": e,
            }
            for c, a, o, t, e in rows
        ]

    def attempt(self, run_id: str, case_id: str, attempt: int) -> dict[str, Any] | None:
        """One stored attempt: output, trajectory, usage, error, and its grades."""
        row = self.con.execute(
            "SELECT output, trajectory, input_tokens, output_tokens, model_calls, cached_calls, "
            "cost_usd, latency_ms, error FROM attempts "
            "WHERE run_id = ? AND case_id = ? AND attempt = ?",
            [run_id, case_id, attempt],
        ).fetchone()
        if row is None:
            return None
        grades = self.con.execute(
            "SELECT grader, metric, value, passed, reason FROM grades "
            "WHERE run_id = ? AND case_id = ? AND attempt = ? ORDER BY grader, metric",
            [run_id, case_id, attempt],
        ).fetchall()
        return {
            "output": json.loads(row[0]),
            "trajectory": json.loads(row[1]),
            "input_tokens": row[2],
            "output_tokens": row[3],
            "model_calls": row[4],
            "cached_calls": row[5],
            "cost_usd": row[6],
            "latency_ms": row[7],
            "error": row[8],
            "grades": [
                {"grader": g, "metric": m, "value": v, "passed": p, "reason": r}
                for g, m, v, p, r in grades
            ],
        }

    def totals(self, run_id: str) -> dict[str, Any]:
        row = self.con.execute(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(model_calls), "
            "SUM(cached_calls), SUM(cost_usd), AVG(latency_ms), "
            "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM attempts WHERE run_id = ?",
            [run_id],
        ).fetchone()
        assert row is not None
        keys = [
            "attempts",
            "input_tokens",
            "output_tokens",
            "model_calls",
            "cached_calls",
            "cost_usd",
            "mean_latency_ms",
            "errors",
        ]
        return dict(zip(keys, row, strict=True))
