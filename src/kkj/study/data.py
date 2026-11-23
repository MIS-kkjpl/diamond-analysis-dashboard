"""
Monthly sales per product brick for the demand study (kkj.study).

Reuses the engine's definitions: brick group = kkj.metrics.brick_turn.add_brick (rule turn_group_levels), a set sold
counts once (kkj.metrics.sets), customer-order sales are left out (as in the demand forecast), net of returns.

For every brick and month it also says whether the month is a GENUINE zero:
    observed   the brick existed by then (first seen in stock or sold on or before that month)
    in_stock   at least one stock reading that month had the brick somewhere in the company
A month with no sale counts as "no demand" only if observed and in stock; before a brick first appears the month
is not data at all; out of stock everywhere it had no chance to sell.
"""
from datetime import date

import pandas as pd

from kkj import rules as rules_mod
from kkj import settings
from kkj.brain.run import forecast_dates
from kkj.db import queries as q
from kkj.metrics import brick_turn as bt
from kkj.metrics import sets

HISTORY_FROM = date(2021, 4, 1)          # start of the ERP sales history
TRAIN_FROM = pd.Period("2021-06", "M")   # Apr-May 2021 were lockdown months (~130 pcs vs ~500): left out of training
ATTRS = ["item_class_group", "purity", "item_name", "category", "weight_band"]


def load(last_month="2026-10"):
    """(sales lines, monthly frame). Monthly frame: one row per (brick_group, month) from TRAIN_FROM to last_month with
    net_pcs (can be < 0), pcs (net, floored at 0), value, observed, in_stock."""
    last = pd.Period(last_month, "M")
    with settings.app_engine().connect() as conn:
        rules = rules_mod.load(conn)
        sales = bt.add_brick(sets.sales_units(q.brick_sales(conn, HISTORY_FROM)), rules)
        dates = q.stock_dates(conn)["snapshot_date"]
        stock = bt.add_brick(sets.stock_units(q.brick_stock(conn, forecast_dates(dates, last.end_time))), rules)
    sales = sales[~sales["order_booked"].fillna(False).astype(bool)]
    sales["month"] = sales["sale_date"].dt.to_period("M")
    sales = sales[sales["month"] <= last]

    months = pd.period_range(TRAIN_FROM, last, freq="M")
    sold = sales.groupby(["brick_group", "month"]).agg(net_pcs=("pcs", "sum"), value=("value", "sum"))
    st = stock[stock["pcs"] > 0].assign(month=pd.to_datetime(stock["snapshot_date"]).dt.to_period("M"))
    held = st.groupby(["brick_group", "month"]).size().rename("readings")

    bricks = sold.index.get_level_values(0).union(held.index.get_level_values(0)).unique()
    idx = pd.MultiIndex.from_product([bricks, months], names=["brick_group", "month"])
    m = pd.DataFrame(index=idx).join(sold).join(held).fillna(0).reset_index()
    m["pcs"] = m["net_pcs"].clip(lower=0)
    m["in_stock"] = m["readings"] > 0

    first_sale = sales.groupby("brick_group")["month"].min()
    first_stock = st.groupby("brick_group")["month"].min()
    first = pd.concat([first_sale, first_stock], axis=1).min(axis=1)
    m["observed"] = m["month"] >= m["brick_group"].map(first)

    attrs = pd.concat([sales[["brick_group"] + ATTRS], stock[["brick_group"] + ATTRS]]).drop_duplicates("brick_group")
    m = m.merge(attrs, on="brick_group", how="left")
    return sales, m.drop(columns="readings")


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "data/study/brick_months.parquet"
    from pathlib import Path
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _, monthly = load()
    monthly.assign(month=monthly["month"].astype(str)).to_parquet(out, index=False)
    print(out, len(monthly), monthly["brick_group"].nunique())
