"""Overview: the headline numbers and their trend."""
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from kkj.dashboard import data
from kkj.dashboard.common import (MUTED_SERIES, SERIES, basis_note, chart, cr, indian, pct, style)
from kkj.metrics import core as m


def fy_start(day):
    """1 April of the financial year containing day."""
    return pd.Timestamp(year=day.year if day.month >= 4 else day.year - 1, month=4, day=1)


def render():
    st.title("Overview")
    stock = m.company_stock(data.stock_latest())
    if stock.empty:
        st.warning("No production stock snapshot yet.")
        return
    as_of = pd.Timestamp(stock["snapshot_date"].iloc[0])
    start = fy_start(as_of)
    series = data.stock_series()
    lines = m.net_sales(data.sales_lines((start - pd.DateOffset(months=12)).date()))
    fy = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= as_of)]
    st.caption(f"Stock as of {as_of:%d %b %Y} · sales {start:%d %b %Y} to {fy['sale_date'].max():%d %b %Y}"
               if len(fy) else f"Stock as of {as_of:%d %b %Y}")

    # ── headline numbers ──
    stock_value = m.value(stock).sum()
    earlier = series[series["snapshot_date"] <= as_of - timedelta(days=7)]
    prior = earlier.iloc[-1] if len(earlier) else None
    days = (as_of - start).days + 1
    avg_stock = m.time_weighted_average(series.set_index("snapshot_date")["value"], start, as_of)
    turn = m.stock_turn(fy["net_value"].sum(), avg_stock, days)
    aged = m.value(stock[stock["age_days"] > 365]).sum() / stock_value
    gross = fy.loc[fy["pcs"] > 0, "net_value"].sum()
    returns = -fy.loc[fy["pcs"] < 0, "net_value"].sum()

    c = st.columns(5)
    c[0].metric("Stock value", cr(stock_value),
                delta=(f"{cr(stock_value - prior['value'])} vs {prior['snapshot_date']:%d %b}"
                       if prior is not None else None), delta_color="off")
    c[1].metric("Pieces in stock", indian(stock["pcs"].sum()), f"{indian(len(stock))} labels", delta_color="off")
    c[2].metric("Net sales this FY", cr(fy["net_value"].sum()),
                f"returns {pct(returns / gross if gross else None)} of gross", delta_color="off")
    c[3].metric("Stock turn (annualised)", f"{turn:.2f}×" if pd.notna(turn) else "–",
                f"{m.days_of_inventory(turn):,.0f} days of stock" if pd.notna(turn) else None, delta_color="off")
    c[4].metric("Value aged over 1 year", pct(aged), cr(m.value(stock[stock['age_days'] > 365]).sum()),
                delta_color="off")
    inside = series[(series["snapshot_date"] >= start) & (series["snapshot_date"] <= as_of)]
    basis_note(f"Value = taxable amount (before GST), returns valued at the sale they reverse. "
               f"Turn = net sales ÷ time-weighted average stock over {days} days, using "
               f"{len(inside)} stock snapshots this FY; retail value on both sides (the ERP holds no cost).")

    # ── net sales by month ──
    st.subheader("Net sales by month")
    monthly = (lines.assign(month=lines["sale_date"].dt.to_period("M").dt.to_timestamp())
               .groupby(["month", "source"], as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum")))
    fig = go.Figure()
    for source, colour, name in (("csv_legacy", MUTED_SERIES, "Legacy file (label value, no returns)"),
                                 ("erp", SERIES[0], "ERP (taxable, net of returns)")):
        part = monthly[monthly["source"] == source]
        if len(part):
            fig.add_bar(x=part["month"], y=part["net"] / 1e7, name=name, marker_color=colour,
                        customdata=part["pcs"], hovertemplate="%{x|%b %Y}<br>₹ %{y:.2f} Cr<br>%{customdata:,.0f} pcs<extra></extra>")
    fig.update_yaxes(ticksuffix=" Cr", tickprefix="₹ ")
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    table = monthly.assign(month=monthly["month"].dt.strftime("%b %Y"), net=(monthly["net"] / 1e7).round(2)) \
        .rename(columns={"net": "net sales (₹ Cr)", "pcs": "net pcs"})
    chart(style(fig, 320), table, "monthly_sales")

    # ── stock value over time ──
    st.subheader("Stock value over time")
    fig = go.Figure(go.Scatter(
        x=series["snapshot_date"], y=series["value"] / 1e7, mode="lines+markers", name="Stock value",
        line=dict(color=SERIES[0], width=2), marker=dict(size=8, color=SERIES[0], line=dict(color="#fcfcfb", width=2)),
        customdata=series[["source", "pcs"]],
        hovertemplate="%{x|%d %b %Y}<br>₹ %{y:.2f} Cr<br>%{customdata[1]:,.0f} pcs · %{customdata[0]}<extra></extra>"))
    fig.update_yaxes(ticksuffix=" Cr", tickprefix="₹ ")
    table = series.assign(snapshot_date=series["snapshot_date"].dt.strftime("%d %b %Y"),
                          value=(series["value"] / 1e7).round(2)).rename(columns={"value": "value (₹ Cr)"})
    chart(style(fig, 300, legend=False), table, "stock_series")
    basis_note("Each point is one snapshot: ERP daily where available, else ERP as-on-date, else a verified "
               "legacy file (label value). Gaps between points are drawn as straight lines.")
