"""Stock health: where the value sits, how old it is, and what isn't on a counter."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from kkj.dashboard import data
from kkj.dashboard.common import ORDINAL, SERIES, basis_note, chart, cr, indian, lakh, pct, style
from kkj.metrics import core as m


def render():
    st.title("Stock health")
    stock = m.company_stock(data.stock_latest()).copy()
    if stock.empty:
        st.warning("No production stock snapshot yet.")
        return
    stock["value"] = m.value(stock)
    stock["age_bucket"] = m.age_bucket(stock["age_days"]).values
    stock["where"] = m.location_type(stock["location"])
    stock["branch_short"] = stock["branch"].str.split(" / ").str[0]

    # ── one filter row scoping everything below ──
    f = st.columns([2, 2, 1])
    branches = f[0].multiselect("Branch", sorted(stock["branch"].dropna().unique()))
    groups = f[1].multiselect("Item group", sorted(stock["item_class_group"].dropna().unique()))
    old_days = f[2].number_input("'Old' means over (days)", min_value=90, max_value=1095, value=365, step=30)
    if branches:
        stock = stock[stock["branch"].isin(branches)]
    if groups:
        stock = stock[stock["item_class_group"].isin(groups)]
    st.caption(f"As of {pd.Timestamp(stock['snapshot_date'].iloc[0]):%d %b %Y} · {indian(len(stock))} labels")

    total = stock["value"].sum()
    old = stock[stock["age_days"] > old_days]
    approval = stock[stock["where"] == "Approval"]
    transit = stock[stock["where"] == "In transit"]
    c = st.columns(5)
    c[0].metric("Stock value", cr(total), f"{indian(stock['pcs'].sum())} pcs", delta_color="off")
    c[1].metric(f"Older than {old_days} days", pct(old["value"].sum() / total if total else None),
                cr(old["value"].sum()), delta_color="off")
    c[2].metric("Median age", f"{stock['age_days'].median():,.0f} days", delta_color="off")
    c[3].metric("On approval", cr(approval["value"].sum()), f"{indian(len(approval))} labels", delta_color="off")
    c[4].metric("In transit", cr(transit["value"].sum()), f"{indian(len(transit))} labels", delta_color="off")

    left, right = st.columns(2)
    with left:
        st.subheader("Value by branch")
        by_branch = stock.groupby("branch", as_index=False).agg(value=("value", "sum"), labels=("barcode", "size"),
                                                                pcs=("pcs", "sum"))
        by_branch = by_branch.sort_values("value")
        fig = go.Figure(go.Bar(x=by_branch["value"] / 1e7, y=by_branch["branch"], orientation="h",
                               marker_color=SERIES[0], customdata=by_branch["labels"],
                               hovertemplate="%{y}<br>₹ %{x:.2f} Cr · %{customdata:,} labels<extra></extra>"))
        fig.update_xaxes(ticksuffix=" Cr", tickprefix="₹ ", showgrid=True, gridcolor="#e1e0d9")
        fig.update_yaxes(showgrid=False)
        chart(style(fig, 360, legend=False),
              by_branch.sort_values("value", ascending=False).assign(value=lambda d: (d["value"] / 1e7).round(2))
              .rename(columns={"value": "value (₹ Cr)"}), "by_branch")
    with right:
        st.subheader("Value by age since labelling")
        by_age = stock.groupby("age_bucket", observed=False, as_index=False).agg(
            value=("value", "sum"), labels=("barcode", "size"))
        by_age["share"] = by_age["value"] / total if total else 0
        fig = go.Figure(go.Bar(x=by_age["age_bucket"].astype(str), y=by_age["value"] / 1e7,
                               marker_color=ORDINAL[: len(by_age)], customdata=by_age[["labels", "share"]],
                               hovertemplate="%{x}<br>₹ %{y:.2f} Cr · %{customdata[1]:.1%}<br>%{customdata[0]:,} labels<extra></extra>"))
        fig.update_yaxes(ticksuffix=" Cr", tickprefix="₹ ")
        chart(style(fig, 360, legend=False),
              by_age.assign(age_bucket=by_age["age_bucket"].astype(str), value=(by_age["value"] / 1e7).round(2),
                            share=(100 * by_age["share"]).round(1))
              .rename(columns={"value": "value (₹ Cr)", "share": "share %"}), "by_age")

    st.subheader("Value by item group and age")
    pivot = stock.pivot_table(index="item_class_group", columns="age_bucket", values="value", aggfunc="sum",
                              observed=False, fill_value=0)
    pivot["Total"] = pivot.sum(axis=1)
    pivot = (pivot.sort_values("Total", ascending=False) / 1e5).round(1)
    st.dataframe(pivot, width="stretch")
    basis_note("₹ lakh. Age = days since the label date (the ERP's own 'Age'). Value = taxable amount before GST. "
               "Customers' own items are left out.")

    st.subheader(f"Oldest stock: over {old_days} days, highest value first")
    cols = ["barcode", "item_name", "category", "item_class_group", "branch_short", "location", "supplier",
            "age_days", "days_from_branch_in", "dia_wt", "value"]
    oldest = old.sort_values("value", ascending=False)[cols].head(200)
    st.dataframe(oldest.assign(value=oldest["value"].map(lakh)), width="stretch", hide_index=True)

    st.subheader("Not on a counter")
    away = stock[stock["where"].isin(["Approval", "In transit", "Repair"])]
    summary = away.groupby(["where", "location"], as_index=False).agg(
        labels=("barcode", "size"), value=("value", "sum"), oldest_days_in_branch=("days_from_branch_in", "max"))
    st.dataframe(summary.sort_values("value", ascending=False).assign(value=lambda d: d["value"].map(lakh)),
                 width="stretch", hide_index=True)
    basis_note("Approval = with customers or staff on approval; days in branch counts from the branch-in date.")
