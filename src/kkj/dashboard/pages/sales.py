"""Sales: what sold, where, how fast, and what came back."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from kkj.dashboard import data
from kkj.dashboard.common import (GROUP_COLOURS, GROUP_ORDER, ORDINAL, SERIES, basis_note, chart, cr, indian,
                                  lakh, pct, style)
from kkj.dashboard.pages.overview import fy_start
from kkj.metrics import core as m


def render():
    st.title("Sales")
    today = pd.Timestamp.today().normalize()
    f = st.columns([2, 2, 2])
    period = f[0].date_input("Period", value=(fy_start(today).date(), today.date()), format="DD/MM/YYYY")
    if not isinstance(period, tuple) or len(period) != 2:
        st.info("Pick a start and an end date.")
        return
    start, end = pd.Timestamp(period[0]), pd.Timestamp(period[1])
    lines = m.net_sales(data.sales_lines(start.date()))
    lines = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= end)]
    branches = f[1].multiselect("Branch", sorted(lines["branch"].dropna().unique()))
    groups = f[2].multiselect("Item group", [g for g in GROUP_ORDER if g in set(lines["item_class_group"])])
    if branches:
        lines = lines[lines["branch"].isin(branches)]
    if groups:
        lines = lines[lines["item_class_group"].isin(groups)]
    if lines.empty:
        st.info("No sales in this selection.")
        return
    legacy = (lines["source"] == "csv_legacy").any()

    sold = lines[lines["pcs"] > 0]
    back = lines[lines["pcs"] < 0]
    gross, returned = sold["net_value"].sum(), -back["net_value"].sum()
    c = st.columns(5)
    c[0].metric("Net sales", cr(lines["net_value"].sum()), f"{indian(lines['net_pcs'].sum())} pcs net", delta_color="off")
    c[1].metric("Returns", cr(returned), f"{pct(returned / gross if gross else None)} of gross · {len(back)} lines",
                delta_color="off")
    c[2].metric("Average sale value", lakh(gross / sold["pcs"].sum() if sold["pcs"].sum() else None), delta_color="off")
    c[3].metric("Median days to sell", f"{sold['age_days_at_sale'].median():,.0f} days",
                "label date → sale", delta_color="off")
    c[4].metric("Bills", indian(sold["bill_no"].nunique()), delta_color="off")
    if legacy:
        basis_note("Part of this period comes from the legacy file: label value (not taxable amount), no returns.")

    st.subheader("Net sales by month and item group")
    monthly = (lines.assign(month=lines["sale_date"].dt.to_period("M").dt.to_timestamp())
               .groupby(["month", "item_class_group"], as_index=False).agg(net=("net_value", "sum"),
                                                                            pcs=("net_pcs", "sum")))
    fig = go.Figure()
    for g in GROUP_ORDER:
        part = monthly[monthly["item_class_group"] == g]
        if len(part):
            fig.add_bar(x=part["month"], y=part["net"] / 1e7, name=g, marker_color=GROUP_COLOURS[g],
                        customdata=part["pcs"],
                        hovertemplate=f"{g}<br>%{{x|%b %Y}}<br>₹ %{{y:.2f}} Cr · %{{customdata:,.0f}} pcs<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(ticksuffix=" Cr", tickprefix="₹ ")
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    chart(style(fig, 340),
          monthly.pivot_table(index="month", columns="item_class_group", values="net", aggfunc="sum", fill_value=0)
          .div(1e7).round(2).reset_index().assign(month=lambda d: d["month"].dt.strftime("%b %Y")), "sales_monthly")

    left, right = st.columns(2)
    with left:
        st.subheader("By branch")
        by_branch = lines.groupby("branch", as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum"))
        by_branch = by_branch.sort_values("net")
        fig = go.Figure(go.Bar(x=by_branch["net"] / 1e7, y=by_branch["branch"], orientation="h", marker_color=SERIES[0],
                               customdata=by_branch["pcs"],
                               hovertemplate="%{y}<br>₹ %{x:.2f} Cr · %{customdata:,.0f} pcs<extra></extra>"))
        fig.update_xaxes(ticksuffix=" Cr", tickprefix="₹ ", showgrid=True, gridcolor="#e1e0d9")
        chart(style(fig, 360, legend=False),
              by_branch.sort_values("net", ascending=False).assign(net=lambda d: (d["net"] / 1e7).round(2))
              .rename(columns={"net": "net sales (₹ Cr)"}), "sales_branch")
    with right:
        st.subheader("Top 10 items")
        by_item = lines.groupby("item_name", as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum"))
        top = by_item.sort_values("net", ascending=False).head(10).sort_values("net")
        fig = go.Figure(go.Bar(x=top["net"] / 1e7, y=top["item_name"], orientation="h", marker_color=SERIES[0],
                               customdata=top["pcs"],
                               hovertemplate="%{y}<br>₹ %{x:.2f} Cr · %{customdata:,.0f} pcs<extra></extra>"))
        fig.update_xaxes(ticksuffix=" Cr", tickprefix="₹ ", showgrid=True, gridcolor="#e1e0d9")
        chart(style(fig, 360, legend=False),
              by_item.sort_values("net", ascending=False).assign(net=lambda d: (d["net"] / 1e7).round(2))
              .rename(columns={"net": "net sales (₹ Cr)"}), "sales_items")

    st.subheader("How long items took to sell")
    sold = sold.assign(age_bucket=m.age_bucket(sold["age_days_at_sale"]).values)
    by_age = sold.groupby("age_bucket", observed=False, as_index=False).agg(pcs=("pcs", "sum"), value=("net_value", "sum"))
    fig = go.Figure(go.Bar(x=by_age["age_bucket"].astype(str), y=by_age["pcs"], marker_color=ORDINAL[: len(by_age)],
                           customdata=by_age["value"] / 1e7,
                           hovertemplate="%{x} on the shelf<br>%{y:,.0f} pcs · ₹ %{customdata:.2f} Cr<extra></extra>"))
    chart(style(fig, 280, legend=False),
          by_age.assign(age_bucket=by_age["age_bucket"].astype(str), value=(by_age["value"] / 1e7).round(2))
          .rename(columns={"value": "₹ Cr"}), "sales_age")

    if lines["salesman"].notna().any():
        st.subheader("Salespeople")
        people = lines.dropna(subset=["salesman"]).groupby("salesman", as_index=False).agg(
            net=("net_value", "sum"), pcs=("net_pcs", "sum"), bills=("bill_no", "nunique"))
        st.dataframe(people.sort_values("net", ascending=False).assign(net=lambda d: d["net"].map(lakh))
                     .rename(columns={"net": "net sales"}), width="stretch", hide_index=True)

    if len(back):
        st.subheader("Returns")
        cols = ["sale_date", "bill_no", "orig_bill_no", "orig_sale_date", "item_name", "branch", "salesman",
                "pcs", "net_value"]
        st.dataframe(back.sort_values("sale_date", ascending=False)[cols]
                     .assign(sale_date=lambda d: d["sale_date"].dt.strftime("%d %b %Y"),
                             net_value=lambda d: d["net_value"].map(lakh))
                     .rename(columns={"sale_date": "returned on", "orig_bill_no": "sold on bill",
                                      "orig_sale_date": "sold on", "net_value": "value"}),
                     width="stretch", hide_index=True)
    basis_note("Value = taxable amount (before GST); a return is valued at the taxable amount of the sale it "
               "reverses and counted in the month it came back.")
