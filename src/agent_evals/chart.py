"""Self-contained SVG charts for the README: the gated-improvement chart (Brief 02 M6) and the
breakdown of why answers are not fully supported. Pure functions of plotted numbers, so the charts
are reproducible from `docs/charts/improvement.json` without the results store, and nothing here
needs a plotting library.

Every rate is drawn on the same 0 to 100% axis with its 95% interval, so no scale is zoomed.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, MUTED, GRID, PAPER = "#1f2328", "#59636e", "#d1d9e0", "#ffffff"
FULLY, CORRECT, HUMAN = "#0969da", "#1a7f37", "#8250df"
STATUS_COLOR = {
    "baseline": MUTED,
    "shipped": "#1a7f37",
    "rejected": "#cf222e",
    "not shipped": "#9a6700",
}


@dataclass(frozen=True)
class Interval:
    mean: float
    low: float
    high: float
    n: int


@dataclass(frozen=True)
class VersionRow:
    label: str
    status: str  # baseline, shipped, rejected or "not shipped"
    fully: Interval | None
    correct: Interval | None
    human_fully: Interval | None = None
    reason: str = ""  # why, shown after the status
    judge_trusted: bool = True  # False draws the judge's rate hollow


def _text(
    x: float,
    y: float,
    s: str,
    size: int = 12,
    fill: str = INK,
    anchor: str = "start",
    weight: str = "normal",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>'
    )


def _axis(x0: float, x1: float, y_top: float, y_bottom: float) -> list[str]:
    """Vertical grid lines and labels at 0, 25, 50, 75 and 100%."""
    out = []
    for i in range(5):
        x = x0 + (x1 - x0) * i / 4
        out.append(
            f'<line x1="{x:.1f}" y1="{y_top:.1f}" x2="{x:.1f}" y2="{y_bottom:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(_text(x, y_bottom + 14, f"{25 * i}%", 11, MUTED, "middle"))
    return out


def _dot(
    x0: float, x1: float, y: float, iv: Interval, color: str, shape: str = "circle"
) -> list[str]:
    def px(v: float) -> float:
        return x0 + (x1 - x0) * max(0.0, min(1.0, v))

    lo, hi, m = px(iv.low), px(iv.high), px(iv.mean)
    out = [
        f'<line x1="{lo:.1f}" y1="{y:.1f}" x2="{hi:.1f}" y2="{y:.1f}" stroke="{color}" '
        'stroke-width="2"/>',
        f'<line x1="{lo:.1f}" y1="{y - 4:.1f}" x2="{lo:.1f}" y2="{y + 4:.1f}" stroke="{color}" '
        'stroke-width="2"/>',
        f'<line x1="{hi:.1f}" y1="{y - 4:.1f}" x2="{hi:.1f}" y2="{y + 4:.1f}" stroke="{color}" '
        'stroke-width="2"/>',
    ]
    if shape == "diamond":
        out.append(
            f'<path d="M{m:.1f},{y - 6:.1f} L{m + 6:.1f},{y:.1f} L{m:.1f},{y + 6:.1f} '
            f'L{m - 6:.1f},{y:.1f} Z" fill="{PAPER}" stroke="{color}" stroke-width="2"/>'
        )
    elif shape == "hollow":
        out.append(
            f'<circle cx="{m:.1f}" cy="{y:.1f}" r="5" fill="{PAPER}" stroke="{color}" '
            'stroke-width="2"/>'
        )
    else:
        out.append(f'<circle cx="{m:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
    return out


def _legend(x: float, y: float, items: list[tuple[str, str, str]]) -> list[str]:
    out = []
    for label, color, shape in items:
        if shape == "diamond":
            out.append(
                f'<path d="M{x + 6:.1f},{y - 6:.1f} L{x + 12:.1f},{y:.1f} L{x + 6:.1f},{y + 6:.1f} '
                f'L{x:.1f},{y:.1f} Z" fill="{PAPER}" stroke="{color}" stroke-width="2"/>'
            )
        elif shape == "hollow":
            out.append(
                f'<circle cx="{x + 6:.1f}" cy="{y:.1f}" r="5" fill="{PAPER}" '
                f'stroke="{color}" stroke-width="2"/>'
            )
        else:
            out.append(f'<circle cx="{x + 6:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
        out.append(_text(x + 18, y + 4, label, 12, INK))
        x += 26 + 7.0 * len(label)
    return out


def _rows(
    rows: list[VersionRow], x_label: float, x0: float, x1: float, y: float, step: float
) -> list[str]:
    out = []
    for row in rows:
        out.append(_text(x_label, y + 4, row.label, 13, INK))
        status = f"{row.status}: {row.reason}" if row.reason else row.status
        out.append(_text(x_label, y + 19, status, 11, STATUS_COLOR.get(row.status, MUTED)))
        if row.correct is not None:
            out += _dot(x0, x1, y - 6, row.correct, CORRECT)
        if row.fully is not None:
            out += _dot(
                x0, x1, y + 6, row.fully, FULLY, "circle" if row.judge_trusted else "hollow"
            )
        if row.human_fully is not None:
            out += _dot(x0, x1, y + 18, row.human_fully, HUMAN, "diamond")
        y += step
    return out


def render_improvement(
    holdout: list[VersionRow],
    dev: list[VersionRow],
    holdout_notes: list[str],
    footnotes: list[str],
) -> str:
    """Holdout results per shipped version above, every gated dev iteration below."""
    width, x_label, x0, x1 = 900, 24, 300, 860
    step = 54
    y = 40
    parts = [_text(x_label, y, "Docs agent: gated improvement loop", 18, INK, weight="600")]
    y += 22
    parts.append(
        _text(
            x_label,
            y,
            "Each dot is a rate with its 95% interval on one 0 to 100% scale (Wilson for single "
            "attempts and human labels; bootstrap over cases for dev's 3 attempts).",
            12,
            MUTED,
        )
    )
    y += 24
    parts += _legend(
        x_label,
        y,
        [
            ("correct", CORRECT, "circle"),
            ("fully supported (LLM judge)", FULLY, "circle"),
            ("judge, not trusted on these answers", FULLY, "hollow"),
            ("human labels", HUMAN, "diamond"),
        ],
    )
    y += 34
    parts.append(
        _text(
            x_label,
            y,
            "Holdout: frozen test split (150 cases), run once per shipped version only",
            14,
            INK,
            weight="600",
        )
    )
    y += 12
    top = y
    y += 26
    parts += _rows(holdout, x_label, x0, x1, y, step)
    y += step * len(holdout) - 12
    parts += _axis(x0, x1, top, y)
    y += 30
    for line in holdout_notes:
        parts.append(_text(x_label, y, line, 12, MUTED))
        y += 16
    y += 34
    parts.append(
        _text(
            x_label,
            y,
            "Dev split (130 cases x 3 attempts): every gated iteration",
            14,
            INK,
            weight="600",
        )
    )
    y += 12
    top = y
    y += 26
    parts += _rows(dev, x_label, x0, x1, y, step + 8)
    y += (step + 8) * len(dev) - 12
    parts += _axis(x0, x1, top, y)
    y += 34
    for note in footnotes:
        parts.append(_text(x_label, y, note, 11, MUTED))
        y += 16
    height = y + 12
    return _svg(width, height, parts)


def render_causes(
    causes: list[str],
    series: list[tuple[str, int, int, dict[str, int]]],
    footnotes: list[str],
) -> str:
    """Why answers are not fully supported: per version (label, answered, failing, counts by cause),
    the number of answers carrying each cause, as a share of all answered attempts."""
    width, x_label, x0, x1 = 900, 24, 300, 860
    colors = ["#8c959f", FULLY, "#1a7f37", "#bf8700"]
    y = 40
    parts = [_text(x_label, y, "Why answers are not fully supported", 18, INK, weight="600")]
    y += 22
    parts.append(
        _text(
            x_label,
            y,
            "Share of answered dev attempts (attempt 1) carrying each cause; "
            "one answer can carry several.",
            12,
            MUTED,
        )
    )
    y += 24
    items = [
        (f"{label} ({failing} of {answered} fail)", colors[i % len(colors)], "circle")
        for i, (label, answered, failing, _) in enumerate(series)
    ]
    parts += _legend(x_label, y, items)
    y += 22
    top = y
    bar_h, gap = 10, 8
    for cause in ["any unsupported claim", *causes]:
        y += 12
        parts.append(_text(x_label, y + 4 + (bar_h + 2) * (len(series) - 1) / 2, cause, 13, INK))
        for i, (_label, answered, failing, counts) in enumerate(series):
            value = failing if cause == "any unsupported claim" else counts.get(cause, 0)
            share = value / answered if answered else 0.0
            w = (x1 - x0) * share
            parts.append(
                f'<rect x="{x0:.1f}" y="{y - bar_h / 2:.1f}" width="{w:.1f}" height="{bar_h}" '
                f'fill="{colors[i % len(colors)]}" rx="2"/>'
            )
            parts.append(_text(x0 + w + 6, y + 4, f"{value} ({100 * share:.0f}%)", 11, MUTED))
            y += bar_h + 2
        y += gap
    parts += _axis(x0, x1, top, y)
    y += 34
    for note in footnotes:
        parts.append(_text(x_label, y, note, 11, MUTED))
        y += 16
    return _svg(width, y + 12, parts)


def _svg(width: int, height: float, parts: list[str]) -> str:
    body = "\n  ".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height:.0f}" '
        f'viewBox="0 0 {width} {height:.0f}" role="img">\n'
        f'  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1:.0f}" rx="8" '
        f'fill="{PAPER}" stroke="{GRID}"/>\n  {body}\n</svg>\n'
    )
