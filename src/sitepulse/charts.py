"""Inline SVG charts, built server-side.

No charting library, no frontend build, no JavaScript at all: the dashboard is an
internal tool and a page that is just HTML is far easier to install, trust and
debug than one that needs a bundler. Hover behaviour is done with CSS on grouped
SVG elements, and every chart is followed by a collapsed table of the same
numbers, so nothing is gated behind colour or a pointer.

Colours come from CSS custom properties defined in ``dashboard.css`` (see the
palette notes there), so light and dark mode swap in one place and the chart code
never hard-codes a hex value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from django.utils.html import escape
from django.utils.safestring import mark_safe
from html_classes.html import HtmlDiv, HtmlElement, HtmlRoot, HtmlSpan

# Categorical slots, in fixed order. Never cycled: a chart that would need a
# ninth series folds the tail into "Other" instead.
SERIES_VARS = [f"var(--sp-series-{index})" for index in range(1, 9)]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def compact(value: float | int | None) -> str:
    """1,284 / 12.9K / 4.2M -- for stat tiles and axis ticks.

    Four-digit numbers stay written out: "1,284" is no harder to read than "1.3K"
    and does not throw away the last two digits.
    """
    if value is None:
        return "--"
    value = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e4, "K")):
        if abs(value) >= limit:
            scaled = value / (1e3 if suffix == "K" else limit)
            return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def duration(ms: float | None) -> str:
    if ms is None:
        return "--"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def percent(fraction: float | None, places: int = 1) -> str:
    if fraction is None:
        return "--"
    return f"{fraction * 100:.{places}f}%"


#: Rounded step sizes an axis tick is allowed to take, per power of ten.
_NICE_STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10)


def nice_ceiling(value: float) -> float:
    """Round up to a clean step x 10^n."""
    if value <= 0:
        return 1
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    for step in _NICE_STEPS:
        if fraction <= step + 1e-9:
            return step * (10 ** exponent)
    return 10 ** (exponent + 1)  # pragma: no cover - unreachable


def axis_ticks(peak: float, count: int = 4) -> list[float]:
    """``count`` evenly spaced ticks ending at a clean ceiling above ``peak``.

    Picking the *step* rather than the ceiling is what keeps ticks at round
    numbers -- 0/60/120/180/240 rather than 0/62.5/125/187.5/250.
    """
    if peak <= 0:
        return [0, 1]
    step = nice_ceiling(peak / count)
    return [step * index for index in range(count + 1)]


def _svg(width: int, height: int, *children, css_classes: str = "sp-svg", **attrs) -> HtmlElement:
    return HtmlElement(
        contents=list(children),
        element="svg",
        raw_attributes={"viewBox": f"0 0 {width} {height}", "preserveAspectRatio": "xMidYMid meet"},
        role="img",
        css_classes=css_classes,
        **attrs,
    )


def _tag(name: str, contents=None, **attrs) -> HtmlElement:
    return HtmlElement(contents=contents, element=name, **attrs)


def _text(x: float, y: float, value: str, css_classes: str = "sp-tick", **attrs) -> HtmlElement:
    return _tag("text", escape(value), x=f"{x:.1f}", y=f"{y:.1f}", css_classes=css_classes, **attrs)


def _title(value: str) -> HtmlElement:
    return _tag("title", escape(value))


def bar_path(x: float, y: float, width: float, height: float, radius: float = 4,
             vertical: bool = False) -> str:
    """A bar with its *data end* rounded and its baseline end square."""
    if vertical:
        radius = max(0.0, min(radius, width / 2, height))
        return (
            f"M{x:.1f},{y + height:.1f} V{y + radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + radius:.1f},{y:.1f} "
            f"H{x + width - radius:.1f} "
            f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width:.1f},{y + radius:.1f} "
            f"V{y + height:.1f} Z"
        )
    radius = max(0.0, min(radius, height / 2, width))
    return (
        f"M{x:.1f},{y:.1f} H{x + width - radius:.1f} "
        f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width:.1f},{y + radius:.1f} "
        f"V{y + height - radius:.1f} "
        f"A{radius:.1f},{radius:.1f} 0 0 1 {x + width - radius:.1f},{y + height:.1f} "
        f"H{x:.1f} Z"
    )


def _figure(title: str, chart, table, note: str = "") -> HtmlDiv:
    contents = [HtmlElement(escape(title), element="h2", css_classes="sp-figure-title")]
    if note:
        contents.append(HtmlDiv(escape(note), css_classes="sp-figure-note"))
    contents.append(chart)
    if table is not None:
        contents.append(
            HtmlElement(
                [
                    _tag("summary", "Show the numbers"),
                    table,
                ],
                element="details",
                css_classes="sp-table-view",
            )
        )
    return HtmlDiv(contents, css_classes="sp-figure")


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> HtmlElement:
    head = _tag(
        "thead",
        _tag("tr", [_tag("th", escape(str(name)), scope="col") for name in headers]),
    )
    body = _tag(
        "tbody",
        [_tag("tr", [_tag("td", escape(str(cell))) for cell in row]) for row in rows],
    )
    return _tag("table", [head, body], css_classes="sp-table")


# ---------------------------------------------------------------------------
# time series
# ---------------------------------------------------------------------------


def line_chart(labels: Sequence[str], series: Sequence[dict], title: str,
               width: int = 720, height: int = 260, note: str = "") -> HtmlDiv:
    """A multi-series line chart with a CSS-only hover crosshair.

    ``series`` is ``[{"name": str, "values": [numbers]}, ...]``. One y-axis, always
    -- two measures on different scales get two charts, never a second axis.
    """
    left, right, top, bottom = 52, 16, 16, 30
    plot_w = width - left - right
    plot_h = height - top - bottom
    count = len(labels)

    peak = max((max(entry["values"], default=0) for entry in series), default=0)
    ticks = axis_ticks(peak)
    ceiling = ticks[-1] or 1

    def x_at(index: int) -> float:
        if count <= 1:
            return left + plot_w / 2
        return left + plot_w * index / (count - 1)

    def y_at(value: float) -> float:
        return top + plot_h - (value / ceiling) * plot_h

    children: list = []

    for tick in ticks:
        y = y_at(tick)
        children.append(
            _tag("line", x1=left, y1=f"{y:.1f}", x2=left + plot_w, y2=f"{y:.1f}",
                 css_classes="sp-grid")
        )
        children.append(_text(left - 8, y + 4, compact(tick), css_classes="sp-tick sp-tick-y"))

    step = max(1, count // 6)
    for index in range(0, count, step):
        children.append(
            _text(x_at(index), height - 10, labels[index], css_classes="sp-tick sp-tick-x")
        )

    single = len(series) == 1
    for slot, entry in enumerate(series):
        colour = SERIES_VARS[slot % len(SERIES_VARS)]
        points = " ".join(
            f"{x_at(index):.1f},{y_at(value):.1f}" for index, value in enumerate(entry["values"])
        )
        if single and count > 1:
            area = (
                f"{left},{top + plot_h} " + points + f" {left + plot_w},{top + plot_h}"
            )
            children.append(
                _tag("polygon", points=area, style=f"fill:{colour}", css_classes="sp-area")
            )
        children.append(
            _tag("polyline", points=points, style=f"stroke:{colour}", css_classes="sp-line")
        )
        if count:
            last = len(entry["values"]) - 1
            children.append(
                _tag("circle", cx=f"{x_at(last):.1f}", cy=f"{y_at(entry['values'][last]):.1f}",
                     r="4.5", style=f"fill:{colour}", css_classes="sp-dot")
            )

    # Hover layer: one invisible column per x position carrying a crosshair and a
    # readout. Pure CSS -- see .sp-col:hover in dashboard.css.
    band = plot_w / max(1, count - 1) if count > 1 else plot_w
    for index, label in enumerate(labels):
        centre = x_at(index)
        readout = [f"{label}"] + [
            f"{entry['name']}: {compact(entry['values'][index])}" for entry in series
        ]
        column = [
            _tag("rect", x=f"{max(left, centre - band / 2):.1f}", y=top,
                 width=f"{band:.1f}", height=plot_h, css_classes="sp-hit"),
            _tag("line", x1=f"{centre:.1f}", y1=top, x2=f"{centre:.1f}", y2=top + plot_h,
                 css_classes="sp-crosshair"),
        ]
        for slot, entry in enumerate(series):
            colour = SERIES_VARS[slot % len(SERIES_VARS)]
            column.append(
                _tag("circle", cx=f"{centre:.1f}", cy=f"{y_at(entry['values'][index]):.1f}",
                     r="4", style=f"fill:{colour}", css_classes="sp-hover-dot")
            )
        column.append(_title(" | ".join(readout)))
        children.append(_tag("g", column, css_classes="sp-col"))

    legend = None
    if len(series) > 1:
        legend = HtmlDiv(
            [
                HtmlSpan(
                    [
                        HtmlSpan("", css_classes="sp-key",
                                 style=f"background:{SERIES_VARS[slot % len(SERIES_VARS)]}"),
                        escape(entry["name"]),
                    ],
                    css_classes="sp-legend-item",
                )
                for slot, entry in enumerate(series)
            ],
            css_classes="sp-legend",
        )

    chart = HtmlDiv(
        [item for item in (legend, _svg(width, height, *children)) if item is not None],
        css_classes="sp-chart",
    )
    table = _table(
        ["Date"] + [entry["name"] for entry in series],
        [
            [label] + [compact(entry["values"][index]) for entry in series]
            for index, label in enumerate(labels)
        ],
    )
    return _figure(title, chart, table, note)


# ---------------------------------------------------------------------------
# horizontal bars
# ---------------------------------------------------------------------------


#: Bars never fill the whole track -- the tail is where the value label goes.
_BAR_MAX_FRACTION = 0.86


def bar_list(rows: Sequence[dict], title: str, value_key: str = "value",
             label_key: str = "label", note: str = "",
             extra_columns: Sequence[tuple[str, str]] = ()) -> HtmlDiv:
    """Ranked horizontal bars: the shape for "top pages", "top sources", "by country".

    ``rows`` is ``[{label: str, value: number, ...}]``, already sorted.

    Built from HTML rather than SVG on purpose. A ranked list is often shown two
    or three to a row, and an SVG scaled down to a third of its design width
    scales its *text* down with it, which is how these end up with 5px labels.
    Real text in real elements stays the size it was asked to be, and the bars
    still stretch to whatever width they are given.
    """
    if not rows:
        empty = HtmlDiv("No data for this range.", css_classes="sp-empty")
        return _figure(title, empty, None, note)

    peak = max(row[value_key] for row in rows) or 1
    built = []
    for row in rows:
        value = row[value_key]
        label = str(row[label_key])
        fraction = (value / peak) * _BAR_MAX_FRACTION
        built.append(
            HtmlDiv(
                [
                    HtmlDiv(escape(label), css_classes="sp-bar-label", title=escape(label)),
                    HtmlDiv(
                        [
                            HtmlDiv("", css_classes="sp-bar-fill",
                                    style=f"width:{max(fraction * 100, 0.5):.2f}%"),
                            HtmlSpan(escape(compact(value)), css_classes="sp-bar-value"),
                        ],
                        css_classes="sp-bar-track",
                    ),
                ],
                css_classes="sp-bar-row",
            )
        )

    headers = ["", title] + [name for name, _ in extra_columns]
    table_rows = [
        [str(row[label_key]), compact(row[value_key])]
        + [str(row.get(key, "")) for _, key in extra_columns]
        for row in rows
    ]
    return _figure(
        title,
        HtmlDiv(built, css_classes="sp-bars"),
        _table(headers, table_rows),
        note,
    )


# ---------------------------------------------------------------------------
# histogram
# ---------------------------------------------------------------------------


def histogram(buckets: Sequence[int], boundaries: Sequence[int], title: str,
              width: int = 720, height: int = 220, note: str = "") -> HtmlDiv:
    """The response-time distribution, as the bucket columns actually stored."""
    left, right, top, bottom = 52, 16, 16, 42
    plot_w = width - left - right
    plot_h = height - top - bottom
    peak = max(buckets) if any(buckets) else 1
    ceiling = axis_ticks(peak, count=2)[-1] or 1

    labels = [f"≤{boundaries[0]}"] + [
        f"≤{boundary}" for boundary in boundaries[1:]
    ] + [f">{boundaries[-1]}"]

    children: list = []
    for fraction in (0, 0.5, 1):
        y = top + plot_h - plot_h * fraction
        children.append(
            _tag("line", x1=left, y1=f"{y:.1f}", x2=left + plot_w, y2=f"{y:.1f}",
                 css_classes="sp-grid")
        )
        children.append(
            _text(left - 8, y + 4, compact(ceiling * fraction), css_classes="sp-tick sp-tick-y")
        )

    slot = plot_w / len(buckets)
    bar_w = min(24, slot - 6)
    for index, count in enumerate(buckets):
        bar_h = plot_h * (count / ceiling) if ceiling else 0
        x = left + slot * index + (slot - bar_w) / 2
        y = top + plot_h - bar_h
        children.append(
            _tag(
                "g",
                [
                    _tag("path", d=bar_path(x, y, bar_w, max(bar_h, 1), vertical=True),
                         style="fill:var(--sp-series-1)", css_classes="sp-bar"),
                    _title(f"{labels[index]}ms: {compact(count)} requests"),
                ],
                css_classes="sp-bar-row",
            )
        )
        children.append(
            _text(x + bar_w / 2, height - 22, labels[index],
                  css_classes="sp-tick sp-tick-x", text_anchor="middle")
        )
    children.append(
        _text(left + plot_w / 2, height - 6, "response time (ms)",
              css_classes="sp-axis-title", text_anchor="middle")
    )

    return _figure(
        title,
        HtmlDiv(_svg(width, height, *children), css_classes="sp-chart"),
        _table(["Bucket", "Requests"], [
            [f"{label}ms", compact(count)] for label, count in zip(labels, buckets, strict=True)
        ]),
        note,
    )


# ---------------------------------------------------------------------------
# stat tiles
# ---------------------------------------------------------------------------


def sparkline(values: Sequence[float], width: int = 120, height: int = 28) -> HtmlElement:
    if not values or max(values) == 0:
        return HtmlSpan("", css_classes="sp-spark-empty")
    peak = max(values)
    step = width / max(1, len(values) - 1)
    points = " ".join(
        f"{index * step:.1f},{height - (value / peak) * (height - 4) - 2:.1f}"
        for index, value in enumerate(values)
    )
    return _svg(
        width, height,
        _tag("polyline", points=points, css_classes="sp-spark-line"),
        css_classes="sp-svg sp-spark",
    )


def stat_tile(label: str, value: str, sub: str = "", values: Sequence[float] = (),
              tone: str = "") -> HtmlDiv:
    contents = [
        HtmlDiv(escape(label), css_classes="sp-stat-label"),
        HtmlDiv(escape(value), css_classes="sp-stat-value"),
    ]
    if sub:
        contents.append(HtmlDiv(escape(sub), css_classes="sp-stat-sub"))
    if values:
        contents.append(HtmlDiv(sparkline(list(values)), css_classes="sp-stat-spark"))
    classes = "sp-stat" + (f" sp-stat-{tone}" if tone else "")
    return HtmlDiv(contents, css_classes=classes)


def stat_row(tiles: Sequence[HtmlDiv]) -> HtmlDiv:
    return HtmlDiv(list(tiles), css_classes="sp-stat-row")


def render(element) -> str:
    """Render a builder object for a template, marked safe."""
    return mark_safe(str(element))


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          css_classes: str = "sp-table") -> HtmlElement:
    """A standalone data table, for the pages/sources/errors detail lists."""
    element = _table(headers, rows)
    element.css_classes = css_classes.split(" ")
    return element


__all__ = [
    "HtmlRoot", "bar_list", "compact", "duration", "histogram", "line_chart", "percent",
    "render", "sparkline", "stat_row", "stat_tile", "table",
]
