"""
Chart look and number formats shared by every UI (web app and the Streamlit dashboard).
No UI framework imports here.
"""
import math

import pandas as pd

# Categorical order, checked for lightness, chroma and colour-blind separation of neighbours
SERIES = ["#008c7a", "#d27a12", "#4a6fc4", "#c0473b", "#8452b8", "#5f9a1c"]
# Ordinal teal ramp for ordered classes (age bands, price bands), light to dark
ORDINAL = ["#9fd3c9", "#5fb3a6", "#2a8f80", "#1d5c55", "#103a35"]
MUTED_SERIES = "#c5cad1"                       # de-emphasised series (e.g. legacy-basis bars)
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#161b24", "#3a4250", "#6b7380", "#e6e8ec", "#c9cdd4", "#ffffff"

# Item groups keep the same colour on every chart (colour follows the entity)
GROUP_ORDER = ["Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND", "Broken Dia Jew"]
GROUP_COLOURS = dict(zip(GROUP_ORDER, SERIES))


def indian(n, decimals=0):
    """1234567.8 -> '12,34,568' (Indian digit grouping)."""
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "–"
    neg, n = n < 0, abs(n)
    whole, frac = f"{n:.{decimals}f}".split(".") if decimals else (f"{n:.0f}", "")
    head, tail = whole[:-3], whole[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    out = (head + "," + tail) if head else tail
    return ("-" if neg else "") + out + (f".{frac}" if frac else "")


def money(x):
    """An amount for people: '₹6.59 Cr', '₹22.4 L', '₹48K'."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    x = float(x)
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.1f} L"
    return f"₹{x / 1e3:.0f}K" if abs(x) >= 1e3 else f"₹{x:.0f}"


def pct(x, decimals=1):
    return "–" if x is None or pd.isna(x) else f"{100 * x:.{decimals}f}%"


def style(fig, height=300, legend=True):
    """Recessive chrome: hairline grid, no zero line, system sans, legend above the plot."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=30 if legend else 10, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family='"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif', size=12, color=INK),
        hoverlabel=dict(bgcolor="white", bordercolor=GRID, font=dict(color=INK)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="",
                    font=dict(color=INK_2)),
        showlegend=legend, bargap=0.35, bargroupgap=0.1,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False, title_text="")
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, linecolor=AXIS, tickfont=dict(color=MUTED),
                     zeroline=False, title_text="")
    fig.update_traces(selector=dict(type="bar"), marker_line_width=0, marker_cornerradius=4)
    return fig
