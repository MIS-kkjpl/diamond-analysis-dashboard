"""Shared look and formatting for the dashboard: palette, chart styling, Indian number formats."""
import math

import pandas as pd
import streamlit as st

# Reference palette (validated categorical order; see the dataviz skill's palette.md)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
# Ordinal blue ramp for ordered classes (age buckets, price bands): steps 250..650
ORDINAL = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
MUTED_SERIES = "#c3c2b7"                       # de-emphasised series (e.g. legacy-basis bars)
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

# Item groups keep the same colour on every chart (colour follows the entity)
GROUP_ORDER = ["Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND", "Broken Dia Jew"]
GROUP_COLOURS = dict(zip(GROUP_ORDER, SERIES))


THEME_CSS = """
<style>
.block-container {padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1400px;}
header[data-testid="stHeader"] {height: 0; background: transparent;}
[data-testid="stToolbar"], footer {visibility: hidden;}
h1 {font-size: 1.55rem !important; font-weight: 650 !important; margin-bottom: .2rem !important;}
h2, h3 {font-size: 1.05rem !important; font-weight: 600 !important; margin-top: .6rem !important;}
[data-testid="stMetric"] {background: #fcfcfb; border: 1px solid rgba(11,11,11,.08); border-radius: 12px;
    padding: .7rem .9rem;}
[data-testid="stMetricLabel"] p {font-size: .78rem; color: #52514e;}
[data-testid="stMetricValue"] {font-size: 1.45rem; font-weight: 650;}
[data-testid="stMetricDelta"] {font-size: .78rem;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 12px; background: #fcfcfb;}
[data-testid="stSidebar"] {background: #f4f3ef;}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {font-size: .85rem;}
.kk-pill {display: inline-block; padding: .12rem .6rem; border-radius: 999px; font-size: .78rem;
    background: #eef4fd; color: #1c5cab; margin-right: .35rem; border: 1px solid #cde2fb;}
.kk-live {background: #e9f6e9; color: #006300; border-color: #bfe3bf;}
.kk-meta {font-size: .8rem; color: #52514e;}
.kk-title {font-size: 1.02rem; font-weight: 620; color: #0b0b0b; margin: .15rem 0;}
.kk-impact {font-size: 1.35rem; font-weight: 650; color: #0b0b0b; text-align: right; line-height: 1.2;}
.kk-impact-sub {font-size: .76rem; color: #52514e; text-align: right;}
</style>
"""


def apply_theme():
    """Dashboard look: tight spacing, card metrics, quiet chrome (call once per run)."""
    st.markdown(THEME_CSS, unsafe_allow_html=True)


def pill(text, live=False):
    return f'<span class="kk-pill{" kk-live" if live else ""}">{text}</span>'


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


def cr(x, decimals=2):
    """Rupees -> '₹ 240.07 Cr'."""
    return "–" if x is None or pd.isna(x) else f"₹ {x / 1e7:,.{decimals}f} Cr"


def lakh(x, decimals=2):
    """Rupees -> '₹ 3.39 L'."""
    return "–" if x is None or pd.isna(x) else f"₹ {x / 1e5:,.{decimals}f} L"


def pct(x, decimals=1):
    return "–" if x is None or pd.isna(x) else f"{100 * x:.{decimals}f}%"


def style(fig, height=320, legend=True):
    """Recessive chrome: hairline grid, no zero line, system sans, legend above the plot."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36 if legend else 12, b=8),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif', size=13, color=INK),
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


def chart(fig, table, key):
    """A chart with its table view one click away (values never live only in tooltips)."""
    st.plotly_chart(fig, width="stretch", key=key, config={"displayModeBar": False})
    with st.expander("Table"):
        st.dataframe(table, width="stretch", hide_index=True)


def basis_note(text):
    st.caption(text)
