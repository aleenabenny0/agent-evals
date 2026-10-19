"""The online dashboard (phase 7 part 4): one static page, docs/dashboard/index.html.

It reads only the stored runs and what the monitor wrote, so it can be rebuilt at any time and
published as it is (GitHub Pages). For each alert metric, a small chart of every day with its
95% interval and graded n, each over a band showing where a normal day of that n falls; then the
alert table, the judge's trust flags, and each day's five least-supported answers with the
judge's reasons. Rates are drawn from zero, up is always worse, and latency and tokens are shown
in their own units (geometric means), not logs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from math import exp
from pathlib import Path
from typing import Any

import numpy as np

from agent_evals.chart import FONT, GRID, INK, MUTED
from agent_evals.monitor import ALERT_FAMILY, DayValues, Spec, shown_label
from agent_evals.simulate import TrafficQuestion
from agent_evals.stats.core import Estimate, bootstrap_ci, wilson_ci
from agent_evals.store.db import ResultsStore

DASHBOARD_PATH = Path("docs/dashboard/index.html")
REFERENCE_COLOR, CHECKED_COLOR, ALERT_COLOR, BAND = "#0969da", INK, "#cf222e", "#ddf4ff"
EXTRA = Spec("fabricated_citation", "rate", 0.0, "cited answers with a fabricated citation")
RANGE_DRAWS = 2_000  # resampled days behind each normal range


def _label(spec: Spec) -> str:
    return shown_label(spec)


def normal_range(
    spec: Spec, pool: Sequence[float], n: int, seed: tuple[int, ...]
) -> tuple[float, float] | None:
    """Where one normal day's value falls 95% of the time, in the units shown: the 2.5th and
    97.5th percentiles of the mean of n values drawn at random from the reference days' pooled
    values, over RANGE_DRAWS draws. Unlike the interval of the pooled mean, this is as wide as
    one day of that n varies, so a normal day falls inside it 19 times in 20."""
    if not pool or n == 0:
        return None
    rng = np.random.default_rng(seed)
    values = np.asarray(pool, dtype=float)
    means = values[rng.integers(0, values.size, size=(RANGE_DRAWS, n))].mean(axis=1)
    lo, hi = (float(v) for v in np.percentile(means, [2.5, 97.5]))
    return (exp(lo), exp(hi)) if spec.name in ("latency", "tokens") else (lo, hi)


@dataclass
class DayPoint:
    day: int
    run_id: str
    reference: bool
    values: DayValues
    report: dict[str, Any] | None  # the monitor's report, for a checked day
    worst: list[dict[str, str]]  # its five least-supported answers


def _values(day: DayValues, spec: Spec) -> list[float]:
    return day.fabricated if spec.name == EXTRA.name else day.values[spec.name]


def interval(spec: Spec, values: Sequence[float]) -> Estimate | None:
    """A rate's Wilson interval, or a mean's bootstrap interval (on the log scale for latency and
    tokens, which `_shown` turns back into their units)."""
    if not values:
        return None
    if spec.kind == "rate":
        return wilson_ci(int(sum(values)), len(values))
    return bootstrap_ci(values)


def _shown(spec: Spec, e: Estimate) -> tuple[float, float, float]:
    if spec.name in ("latency", "tokens"):
        return exp(e.mean), exp(e.low), exp(e.high)
    return e.mean, e.low, e.high


def _fmt(spec: Spec, v: float) -> str:
    if spec.kind == "rate":
        return f"{v * 100:.0f}%"
    if spec.name == "latency":
        return f"{v / 1000:.1f} s"
    if spec.name == "tokens":
        return f"{v / 1000:.1f}k"
    return f"{v:.1f}"


def metric_chart(spec: Spec, points: Sequence[DayPoint], index: int = 0) -> str:
    """One metric across the days: a dot and 95% interval per day, each over the range a normal
    day of its n shows. A reference day's range comes from the other reference days."""
    width, height, left, right, top, bottom = 340, 170, 46, 12, 14, 34
    shown = [(p, interval(spec, _values(p.values, spec))) for p in points]
    estimates = [e for _, e in shown if e is not None]
    bands = [
        normal_range(
            spec,
            [x for o in points if o.reference and o is not p for x in _values(o.values, spec)],
            len(_values(p.values, spec)),
            (index, p.day),
        )
        for p in points
    ]
    highs = [_shown(spec, e)[2] for e in estimates] + [b[1] for b in bands if b]
    lows = [_shown(spec, e)[1] for e in estimates] + [b[0] for b in bands if b]
    if spec.kind == "rate":
        y_min, y_max = 0.0, max(0.1, max(highs, default=0.1) * 1.15)
    else:
        y_min, y_max = min(lows, default=0.0) * 0.9, max(highs, default=1.0) * 1.1
    days = [p.day for p in points] or [1]
    x_lo, x_hi = min(days) - 0.5, max(days) + 0.5

    def x(day: float) -> float:
        return left + (day - x_lo) / (x_hi - x_lo) * (width - left - right)

    def y(v: float) -> float:
        return top + (1 - (v - y_min) / ((y_max - y_min) or 1)) * (height - top - bottom)

    parts = [
        f'<line x1="{left}" y1="{y(y_min):.1f}" x2="{width - right}" '
        f'y2="{y(y_min):.1f}" stroke="{GRID}"/>'
    ]
    for frac in (0.0, 0.5, 1.0):  # the bottom is labelled too: only rates start at zero
        v = y_min + frac * (y_max - y_min)
        if frac:
            parts.append(
                f'<line x1="{left}" y1="{y(v):.1f}" x2="{width - right}" y2="{y(v):.1f}" '
                f'stroke="{GRID}" stroke-dasharray="2 3"/>'
            )
        parts.append(
            f'<text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{MUTED}">{escape(_fmt(spec, v))}</text>'
        )
    step = (width - left - right) / (x_hi - x_lo)
    for p, band in zip(points, bands, strict=True):
        if band is None:
            continue
        lo, hi = band
        parts.append(
            f'<rect x="{x(p.day) - 0.3 * step:.1f}" y="{y(hi):.1f}" width="{0.6 * step:.1f}" '
            f'height="{max(1.0, y(lo) - y(hi)):.1f}" fill="{BAND}"><title>a normal day of n '
            f"{len(_values(p.values, spec))}, 95% of the time: {escape(_fmt(spec, lo))} to "
            f"{escape(_fmt(spec, hi))}</title></rect>"
        )
    for p, e in shown:
        alerted = bool(p.report and spec.name in p.report.get("alerts", []))
        color = ALERT_COLOR if alerted else (REFERENCE_COLOR if p.reference else CHECKED_COLOR)
        parts.append(
            f'<text x="{x(p.day):.1f}" y="{height - bottom + 16}" text-anchor="middle" '
            f'font-size="11" fill="{MUTED}">{p.day}</text>'
        )
        if e is None:
            continue
        mean, lo, hi = _shown(spec, e)
        tip = f"day {p.day}: {_fmt(spec, mean)} ({_fmt(spec, lo)} to {_fmt(spec, hi)}), n {e.n}"
        parts.append(
            f'<line x1="{x(p.day):.1f}" y1="{y(hi):.1f}" x2="{x(p.day):.1f}" y2="{y(lo):.1f}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
        parts.append(
            f'<circle cx="{x(p.day):.1f}" cy="{y(mean):.1f}" r="{5 if alerted else 4}" '
            f'fill="{color}"><title>{escape(tip)}</title></circle>'
        )
        parts.append(
            f'<text x="{x(p.day):.1f}" y="{height - 4}" text-anchor="middle" font-size="9" '
            f'fill="{MUTED}">n {e.n}</text>'
        )
    body = "".join(parts)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(_label(spec))}" '
        f'font-family="{FONT}" xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


def least_supported(
    store: ResultsStore, run_id: str, traffic: dict[str, TrafficQuestion], k: int = 5
) -> list[dict[str, str]]:
    """The day's k answers the judge found least supported (core claims unsupported first),
    with the judge's reason."""
    reasons = store.grade_reasons(run_id, "judge")
    outputs = {a["case_id"]: a["output"] for a in store.answered_attempts(run_id)}
    worst = []
    for (case, attempt), grades in sorted(reasons.items()):
        full = grades.get("judge_supported")
        if attempt != 1 or full is None or full[0] == 1.0:
            continue
        core = grades.get("judge_core_supported", (1.0, ""))[0]
        worst.append((core, case, full[1]))
    worst.sort()
    out = []
    for _, case, reason in worst[:k]:
        q = traffic.get(case)
        answer = str(outputs.get(case, {}).get("answer", ""))
        out.append(
            {
                "case": case,
                "question": q.question if q else "",
                "answer": answer if len(answer) <= 400 else answer[:400] + " ...",
                "reason": reason,
            }
        )
    return out


CSS = (
    """
:root { --ink: #1f2328; --muted: #59636e; --line: #d1d9e0; --paper: #ffffff; --alert: #cf222e;
  --ok: #1a7f37; --soft: #f6f8fa; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.5 FONT_STACK; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 32px 0 8px; }
p.lede { color: var(--muted); margin: 0 0 16px; }
.banner { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px;
  background: var(--soft); }
.banner strong.alert { color: var(--alert); } .banner strong.ok { color: var(--ok); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
  gap: 16px; }
figure { margin: 0; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
figcaption { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
figcaption span { font-weight: 400; color: var(--muted); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
.scroll { overflow-x: auto; }
td.alert { color: var(--alert); font-weight: 600; } .muted { color: var(--muted); }
details { border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin: 8px 0;
  overflow-wrap: break-word; }
summary { cursor: pointer; font-weight: 600; }
blockquote { margin: 6px 0; padding: 4px 10px; border-left: 3px solid var(--line);
  color: var(--muted); }
.key span { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin: 0 4px 0 12px; }
"""
).replace("FONT_STACK", FONT)


def render(
    points: Sequence[DayPoint], reference: dict[str, Any] | None, judge_in_family: bool
) -> str:
    """The whole page."""
    checked = [p for p in points if not p.reference]
    alerting = [p for p in checked if p.report and p.report["alerts"]]
    if reference is None:
        status = "The reference is not frozen yet: days 1 to 5 still run."
    elif alerting:
        days = ", ".join(
            f"day {p.day} ({', '.join(p.report['alerts'])})" for p in alerting if p.report
        )
        status = f'<strong class="alert">Alert:</strong> {escape(days)}.'
    else:
        status = '<strong class="ok">No alert</strong> on any checked day.'
    frozen = (
        f"Reference: days {', '.join(reference['days'])}, frozen "
        f"{reference.get('checked_at', '')}; "
        "the leave-one-day-out check raised no alert."
        if reference
        else ""
    )
    judge = (
        "judge_supported is in the alert family: the judge passed the 30 blind labels, and the "
        "reference stayed quiet with it in the family."
        if judge_in_family
        else "judge_supported is shown but cannot alert until the judge passes Aleena's 30 "
        "blind labels."
    )
    figures = []
    for i, spec in enumerate((*ALERT_FAMILY, EXTRA)):
        floor = "" if spec.name == EXTRA.name else f"alert floor {_floor(spec)}; "
        rule = "own rule, p &lt; 0.01" if spec.name == EXTRA.name else "Holm-corrected family"
        figures.append(
            f"<figure><figcaption>{escape(_label(spec))} <span>({floor}{rule}; up is worse)</span>"
            f"</figcaption>{metric_chart(spec, points, i)}</figure>"
        )
    rows = []
    for p in checked:
        r = p.report or {}
        alerts = ", ".join(r.get("alerts", [])) or "none"
        trust = (r.get("judge") or {}).get("trusted")
        rows.append(
            f'<tr><td>{p.day}</td><td class="muted">{escape(p.run_id)}</td>'
            f'<td class="{"alert" if r.get("alerts") else ""}">{escape(alerts)}</td>'
            f"<td>{'yes' if trust else 'no: a quiet judge_supported does not clear the day'}"
            "</td></tr>"
        )
    table = (
        '<div class="scroll"><table><thead><tr><th>day</th><th>run</th><th>alerts</th>'
        f"<th>judge trusted</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else '<p class="muted">No day after the reference has been checked yet.</p>'
    )
    answers = []
    for p in points:
        items = "".join(
            f"<li><p><strong>{escape(a['question'])}</strong></p><blockquote>{escape(a['answer'])}"
            f'</blockquote><p class="muted">Judge: {escape(a["reason"])}</p></li>'
            for a in p.worst
        )
        answers.append(
            f"<details><summary>Day {p.day}{' (reference)' if p.reference else ''}: the five "
            f"least-supported answers</summary><ol>{items or '<li>none</li>'}</ol></details>"
        )
    key = (
        f'<p class="key muted"><span style="background:{REFERENCE_COLOR}"></span>reference day'
        f'<span style="background:{CHECKED_COLOR}"></span>checked day'
        f'<span style="background:{ALERT_COLOR}"></span>alert'
        f'<span style="background:{BAND};border-radius:2px"></span>where a normal day of that n '
        "falls, 95% of the time (from the reference days)</p>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docs agent monitor</title><style>{CSS}</style></head>
<body><main>
<h1>Docs agent: online monitor</h1>
<p class="lede">Simulated traffic, graded only by checks that need no answer key. Each dot is a
day with its 95% interval and n, the number of answers that metric graded. Rebuilt by
<code>evals dashboard</code>.</p>
<div class="banner"><p>{status}</p><p class="muted">{escape(frozen)} {escape(judge)}</p></div>
<h2>The alert family, day by day</h2>{key}
<div class="grid">{"".join(figures)}</div>
<h2>Checked days</h2>{table}
<h2>What the judge disliked most</h2>{"".join(answers)}
</main></body></html>
"""


def _floor(spec: Spec) -> str:
    if spec.name in ("latency", "tokens"):
        return f"+{(exp(spec.floor) - 1) * 100:.0f}%"
    if spec.kind == "rate":
        return f"+{spec.floor * 100:.0f} points"
    return f"+{spec.floor:g}"
