"""
Chart and table helpers for the web pages: Plotly figures (styled by kkj.viz) as JSON for the
browser, and DataFrames turned into display rows with Indian number formats.
"""
import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from markupsafe import Markup, escape

from kkj.viz import GRID, GROUP_COLOURS, GROUP_ORDER, ORDINAL, SERIES, indian, money, style


def hbar(labels, values_cr, custom=None, hover="", height=None):
    """Horizontal bars, one colour, values in ₹ Cr (largest at the top)."""
    fig = go.Figure(go.Bar(x=values_cr, y=labels, orientation="h", marker_color=SERIES[0], customdata=custom,
                           hovertemplate="%{y}<br>₹%{x:.2f} Cr" + hover + "<extra></extra>"))
    fig.update_xaxes(tickprefix="₹", ticksuffix=" Cr", showgrid=True, gridcolor=GRID)
    fig.update_yaxes(showgrid=False, autorange="reversed")
    return style(fig, height or max(260, 30 * len(labels)), legend=False).to_json()


def ordinal_bars(labels, values, suffix="", prefix="", custom=None, hover="", height=280):
    """Bars for ordered classes (age bands), light to dark."""
    colours = (ORDINAL + [ORDINAL[-1]] * 10)[: len(labels)]
    fig = go.Figure(go.Bar(x=[str(l) for l in labels], y=values, marker_color=colours, customdata=custom,
                           hovertemplate="%{x}<br>" + prefix + "%{y:,.2f}" + suffix + hover + "<extra></extra>"))
    fig.update_yaxes(tickprefix=prefix, ticksuffix=suffix)
    return style(fig, height, legend=False).to_json()


def monthly_by_group(monthly):
    """Stacked monthly bars, one colour per item group (colour follows the group everywhere)."""
    fig = go.Figure()
    for g in GROUP_ORDER:
        part = monthly[monthly["item_class_group"] == g]
        if len(part):
            fig.add_bar(x=part["month"], y=part["net"] / 1e7, name=g, marker_color=GROUP_COLOURS[g],
                        customdata=part["pcs"],
                        hovertemplate=f"{g}<br>%{{x|%b %Y}}<br>₹%{{y:.2f}} Cr · %{{customdata:,.0f}} pcs<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(tickprefix="₹", ticksuffix=" Cr")
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    return style(fig, 320).to_json()


def month_bars(months, values_l, bills):
    """One person's net sales per month in ₹ lakh, one colour (time runs left to right)."""
    fig = go.Figure(go.Bar(x=months, y=values_l, marker_color=SERIES[0], customdata=bills,
                           hovertemplate="%{x|%b %Y}<br>₹%{y:,.1f} L · %{customdata:,} bills<extra></extra>"))
    fig.update_yaxes(tickprefix="₹", ticksuffix=" L")
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    return style(fig, 280, legend=False).to_json()


def two_series(labels, a, b, name_a, name_b, suffix="%"):
    fig = go.Figure()
    fig.add_bar(x=labels, y=a, name=name_a, marker_color=SERIES[0])
    fig.add_bar(x=labels, y=b, name=name_b, marker_color=SERIES[1])
    fig.update_yaxes(ticksuffix=suffix)
    return style(fig, 300).to_json()


def performance_bars(names, perf, custom, hover):
    fig = go.Figure(go.Bar(x=perf, y=names, orientation="h", marker_color=SERIES[0], customdata=custom,
                           hovertemplate="%{y}<br>%{x:.2f}× the company rate at its prices" + hover + "<extra></extra>"))
    fig.add_vline(x=1, line_color=GRID, line_width=2)
    fig.update_xaxes(ticksuffix="×", showgrid=True, gridcolor=GRID)
    fig.update_yaxes(autorange="reversed")
    return style(fig, max(280, 24 * len(names)), legend=False).to_json()


def _cell(value, kind):
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "–"
    if kind == "money":
        return "−" + money(-value) if value < 0 else money(value)
    if kind == "pct":
        return f"{100 * value:.1f}%"
    if kind == "num":
        return indian(float(value))
    if kind == "dec":
        return f"{value:,.2f}"
    if kind == "date":
        return pd.Timestamp(value).strftime("%d %b %Y")
    if kind == "link":                      # (text, href); plain text when the page it leads to is switched off
        text, href = value
        from kkj import features
        if not href or features.blocked(str(href).split("?")[0]):
            return text
        return Markup(f'<a href="{escape(href)}">{escape(text)}</a>')
    return value


def table(df, formats=None, rename=None):
    """{'columns': [...], 'rows': [[...]], 'numeric': [...]} ready for the _table macro."""
    formats = formats or {}
    df = df.copy()
    numeric = [c for c in df.columns if c in formats and formats[c] in ("money", "pct", "num", "dec")
               or (c not in formats and pd.api.types.is_numeric_dtype(df[c]))]
    rows = [[_cell(v, formats.get(c, "num" if c in numeric and isinstance(v, (int, float, np.number)) else None))
             for c, v in zip(df.columns, rec)] for rec in df.itertuples(index=False, name=None)]
    names = [(rename or {}).get(c, str(c).replace("_", " ")) for c in df.columns]
    return {"columns": names, "rows": rows, "numeric": [names[list(df.columns).index(c)] for c in numeric]}


def season_lines(months, series):
    """Monthly index lines, one per key, April -> March (1.0 = an average month). series: {name: [12 values]}."""
    fig = go.Figure()
    for i, (name, values) in enumerate(series.items()):
        fig.add_scatter(x=months, y=values, name=name, mode="lines+markers", line=dict(width=2, color=SERIES[i % len(SERIES)]),
                        marker=dict(size=8), hovertemplate=f"{name}<br>%{{x}}: %{{y:.2f}}× an average month<extra></extra>")
    fig.add_hline(y=1, line_color=GRID, line_width=2)
    fig.update_yaxes(ticksuffix="×", rangemode="tozero")
    return style(fig, 320).to_json()
