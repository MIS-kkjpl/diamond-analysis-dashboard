"""Data checks: how fresh the data is and what every load reported."""
import json

import pandas as pd
import streamlit as st

from kkj.dashboard import data

STALE_DAYS = 1      # stock or sales older than this (in days) is flagged


def _status(ok, text):
    return f"{'✅' if ok else '⚠️'} {text}"


def render():
    st.title("Data checks")
    today = pd.Timestamp.today().normalize()
    runs, loads, snaps = data.stock_runs(), data.sales_loads(), data.snapshot_status()

    stock_ok = runs[runs["status"] == "success"]
    last_stock = pd.Timestamp(stock_ok["started_ist"].max()) if len(stock_ok) else None
    sales_ok = loads[(loads["status"] == "success") & (loads["source"] == "erp")]
    last_sales_to = pd.Timestamp(sales_ok["date_to"].max()) if len(sales_ok) else None

    c = st.columns(3)
    c[0].markdown(_status(last_stock is not None and (today - last_stock.normalize()).days <= STALE_DAYS,
                          f"**Stock** last loaded {last_stock:%d %b %Y %H:%M}" if last_stock is not None
                          else "**Stock** never loaded"))
    c[1].markdown(_status(last_sales_to is not None and (today - last_sales_to).days <= STALE_DAYS,
                          f"**Sales** loaded up to {last_sales_to:%d %b %Y}" if last_sales_to is not None
                          else "**Sales** never loaded from the ERP"))
    failed_recent = runs.head(5)["status"].eq("failed").sum() + loads.head(5)["status"].eq("failed").sum()
    c[2].markdown(_status(failed_recent == 0, f"**{failed_recent}** failed among the latest loads"))

    st.subheader("Stock pipeline runs")
    if len(runs):
        latest_dq = runs.loc[runs["status"] == "success", "dq_warnings"].head(1)
        if len(latest_dq) and latest_dq.iloc[0]:
            dq = latest_dq.iloc[0] if isinstance(latest_dq.iloc[0], dict) else json.loads(latest_dq.iloc[0])
            st.caption("Latest data-quality warnings (never block a load): " +
                       " · ".join(f"{k.replace('_', ' ')}: {v['count']}" for k, v in dq.items()))
    st.dataframe(runs.drop(columns=["dq_warnings"]), width="stretch", hide_index=True)

    st.subheader("Sales loads")
    st.dataframe(loads.drop(columns=["dq_warnings"]), width="stretch", hide_index=True)
    st.caption("A failed load is never left half-written: the range is replaced in one transaction, and "
               "a later successful load of the same range supersedes it.")

    st.subheader("Past-date stock snapshots")
    st.dataframe(snaps, width="stretch", hide_index=True)
    st.caption("Quarantined snapshots failed a check (labels dated after the snapshot, items already sold "
               "still listed, or an identical copy of another file) and are not used anywhere.")
