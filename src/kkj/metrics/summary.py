"""
Headline numbers for the overview, computed once from the database (any UI can show them).
"""
from datetime import timedelta

import pandas as pd

from kkj.db import queries as q
from kkj.metrics import core as m


def fy_start(day):
    """1 April of the financial year containing day."""
    day = pd.Timestamp(day)
    return pd.Timestamp(year=day.year if day.month >= 4 else day.year - 1, month=4, day=1)


def headline(conn, aged_days=365):
    """Stock, sales, turn and ageing as of the latest snapshot, plus the series behind the charts."""
    stock = m.company_stock(q.stock_latest(conn))
    if stock.empty:
        return None
    as_of = pd.Timestamp(stock["snapshot_date"].iloc[0])
    start = fy_start(as_of)
    series = q.stock_series(conn)
    lines = m.net_sales(q.sales_lines(conn, (start - pd.DateOffset(months=12)).date()))
    fy = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= as_of)]
    value = m.value(stock)
    stock_value = value.sum()
    earlier = series[series["snapshot_date"] <= as_of - timedelta(days=7)]
    prior = earlier.iloc[-1] if len(earlier) else None
    days = (as_of - start).days + 1
    avg_stock = m.time_weighted_average(series.set_index("snapshot_date")["value"], start, as_of)
    turn = m.stock_turn(fy["net_value"].sum(), avg_stock, days)
    gross = fy.loc[fy["pcs"] > 0, "net_value"].sum()
    returns = -fy.loc[fy["pcs"] < 0, "net_value"].sum()
    aged = value[stock["age_days"] > aged_days].sum()
    monthly = (lines.assign(month=lines["sale_date"].dt.to_period("M").dt.to_timestamp())
               .groupby(["month", "source"], as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum")))
    return {
        "as_of": as_of, "fy_start": start, "sales_to": fy["sale_date"].max() if len(fy) else None,
        "stock_value": stock_value, "pcs": stock["pcs"].sum(), "labels": len(stock),
        "change_7d": stock_value - prior["value"] if prior is not None else None,
        "change_vs": prior["snapshot_date"] if prior is not None else None,
        "net_sales_fy": fy["net_value"].sum(), "returns_share": returns / gross if gross else None,
        "turn": turn, "days_of_stock": m.days_of_inventory(turn), "fy_days": days,
        "snapshots_in_fy": int(((series["snapshot_date"] >= start) & (series["snapshot_date"] <= as_of)).sum()),
        "aged_value": aged, "aged_share": aged / stock_value if stock_value else None, "aged_days": aged_days,
        "monthly": monthly, "series": series,
    }
