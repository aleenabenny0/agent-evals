from __future__ import annotations

import xml.etree.ElementTree as ET

from agent_evals.chart import Interval, VersionRow, render_causes, render_improvement

SVG = "{http://www.w3.org/2000/svg}"


def texts(svg: str) -> list[str]:
    root = ET.fromstring(svg)  # well-formed XML, or this raises
    assert root.tag == f"{SVG}svg"
    return [t.text or "" for t in root.iter(f"{SVG}text")]


def test_improvement_chart_draws_every_row_with_its_interval() -> None:
    iv = Interval(0.3, 0.2, 0.4, 118)
    rows = [
        VersionRow("v2 baseline", "baseline", iv, Interval(0.95, 0.9, 0.98, 130)),
        VersionRow("v5 & <draft>", "not shipped", iv, None, Interval(0.6, 0.5, 0.7, 90)),
    ]
    svg = render_improvement(rows[:1], rows, ["a note"], ["a footnote"])
    labels = texts(svg)
    assert "v5 & <draft>" in labels  # escaped in the source, intact once parsed
    assert {"baseline", "not shipped", "a note", "a footnote"} <= set(labels)
    assert {"0%", "50%", "100%"} <= set(labels)
    # One circle per judge rate (2 + 3 here, plus 3 in the legend) and one diamond per human rate.
    root = ET.fromstring(svg)
    assert len(list(root.iter(f"{SVG}circle"))) == 8
    assert len([p for p in root.iter(f"{SVG}path")]) == 2  # one human diamond, one in the legend


def test_causes_chart_scales_bars_to_answered_attempts() -> None:
    svg = render_causes(
        ["unquoted"],
        [("v3", 100, 80, {"unquoted": 50}), ("v5", 100, 30, {"unquoted": 20})],
        ["note"],
    )
    labels = texts(svg)
    assert "80 (80%)" in labels and "50 (50%)" in labels and "20 (20%)" in labels
    widths = [float(r.get("width", 0)) for r in ET.fromstring(svg).iter(f"{SVG}rect")][1:]
    assert widths[0] > widths[2] > widths[3] > 0  # 80% bar longest, 20% bar shortest


def test_untrusted_judge_rates_are_hollow_and_reasons_are_shown() -> None:
    iv = Interval(0.6, 0.5, 0.7, 119)
    rows = [VersionRow("v5", "not shipped", iv, None, None, "judge not trusted", False)]
    svg = render_improvement([], rows, [], [])
    assert "not shipped: judge not trusted" in texts(svg)
    hollow = [
        c
        for c in ET.fromstring(svg).iter(f"{SVG}circle")
        if c.get("fill") == "#ffffff" and c.get("stroke") == "#0969da"
    ]
    assert len(hollow) == 2  # the row's dot and its legend entry
