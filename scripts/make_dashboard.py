"""Write the Grafana dashboard, so its panels are reviewed as code rather than as generated JSON.

uv run python scripts/make_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path("deploy/grafana/dashboards/docs-agent.json")
SOURCE = {"type": "prometheus", "uid": "prometheus"}
FAILED = 'outcome=~"provider_error|agent_failed|timeout"'
LATENCY = "sum by (le) (rate(ask_latency_seconds_bucket[5m]))"
# Title, unit, and each query with its legend. The first row is the four golden signals, the second
# what is particular to an LLM service.
PANELS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "Questions per second",
        "reqps",
        [("sum by (outcome) (rate(ask_requests_total[1m]))", "{{outcome}}")],
    ),
    (
        "Questions with no answer",
        "percentunit",
        [
            (
                f"sum(rate(ask_requests_total{{{FAILED}}}[2m])) / "
                "sum(rate(ask_requests_total[2m]))",
                "failed",
            )
        ],
    ),
    (
        "Answer latency",
        "s",
        [
            (f"histogram_quantile(0.5, {LATENCY})", "p50"),
            (f"histogram_quantile(0.95, {LATENCY})", "p95"),
        ],
    ),
    ("Answers in flight", "short", [("ask_in_flight", "in flight"), ("ask_capacity", "capacity")]),
    (
        "Spend per hour, by prompt version",
        "currencyUSD",
        [("sum by (prompt_version) (rate(llm_cost_usd_total[5m])) * 3600", "{{prompt_version}}")],
    ),
    (
        "Abstentions, by prompt version",
        "percentunit",
        [
            (
                'sum by (prompt_version) (rate(ask_requests_total{outcome="abstained"}[5m])) / '
                "sum by (prompt_version) (rate(ask_requests_total[5m]))",
                "{{prompt_version}}",
            )
        ],
    ),
    (
        "Traffic by prompt version and role",
        "reqps",
        [
            (
                "sum by (prompt_version, role) (rate(ask_requests_total[1m]))",
                "{{prompt_version}} {{role}}",
            )
        ],
    ),
    (
        "Failovers and provider failures",
        "short",
        [
            ("sum by (provider) (rate(gateway_failovers_total[5m]))", "failover to {{provider}}"),
            (
                "sum by (provider) (rate(gateway_provider_failures_total[5m]))",
                "{{provider}} failed",
            ),
            ("gateway_circuit_open", "{{provider}} circuit open"),
        ],
    ),
]


def panel(n: int, title: str, unit: str, queries: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": n + 1,
        "title": title,
        "type": "timeseries",
        "datasource": SOURCE,
        "gridPos": {"h": 8, "w": 6, "x": (n % 4) * 6, "y": (n // 4) * 8},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"datasource": SOURCE, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(queries)
        ],
    }


def dashboard() -> dict[str, Any]:
    return {
        "uid": "docs-agent",
        "title": "Docs agent",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": [panel(n, *spec) for n, spec in enumerate(PANELS)],
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard(), indent=1) + "\n", encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
