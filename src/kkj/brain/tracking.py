"""
Did the branches follow the ideal stock, and did it pay? Measured from what happened in the stock and sales, not
from buttons (hardly anyone approves a card; the stock moves anyway).

  start     an ideal stock refresh (brain.ideal_stock as_of): per product at each showroom, what was held, the
            ideal, the 12-month forecast and the average held over the year before (held_12m)
  gap       ideal - held at the start, counted only when worth recommending (ideal_stock.status: the band and the
            smallest change)
  closed    (held now - held at start) / gap: 1 = got to the ideal; below 0 = went the other way
  verdict   followed (closed >= 1/2), partly (>= 1/10), not yet (within a tenth either way: drift), other way
            (<= -1/10); no gap = within the band
  expected  sales over the days since the start: the forecast pro rata, scaled by the stock actually held over the
            period (the measured sales response to pieces held, rule fc_held_effect)
  sold      sales since the start, counted as the forecast counts them (sets as one, customer orders out when the
            forecast leaves them out)

Sold against expected, followed vs not, is the test of the ideal stock. It needs a few months: a week of sales of
a product at a branch is mostly chance.
"""
import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj.db import queries as q
from kkj.metrics import brick_turn as bt, forecast as fc, ideal_stock as ist, sets

MOVED = 0.1     # a tenth of the gap: less is drift (a few pieces sold), not a move
VERDICTS = {"followed": ("Followed", "green"), "partly": ("Partly", "lime"), "not_yet": ("Not yet", "secondary"),
            "other_way": ("Other way", "orange"), "no_gap": ("Within range", "blue")}


def starts(conn):
    """The ideal stock refreshes that can be a start (oldest first)."""
    return [d for d in pd.read_sql(text("SELECT DISTINCT as_of FROM brain.ideal_stock ORDER BY 1"), conn)["as_of"]]


def follow_ideal(conn, rules, start=None):
    """One row per product at a showroom: held / ideal / forecast at the start, held now, gap closed, verdict, sold
    and expected since. Returns (rows, start, as_of now)."""
    s = starts(conn)
    if not s:
        return pd.DataFrame(), None, None
    start = pd.Timestamp(start) if start is not None else pd.Timestamp(s[0])
    start = max([pd.Timestamp(d) for d in s if pd.Timestamp(d) <= start] or [pd.Timestamp(s[0])])
    b = q.branches(conn)
    shops = set(b.loc[(b["kind"] == "showroom") & b["active"], "erp_branch"])
    has_held = "held_12m" in pd.read_sql(text("""SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'brain' AND table_name = 'ideal_stock'"""), conn)["column_name"].tolist()
    base = pd.read_sql(text(f"""SELECT item_name, scope AS branch, sum(current_pcs) held_start, sum(ideal_pcs) ideal_start,
                                       sum(annual) annual, {"max(held_12m)" if has_held else "NULL"} held_12m
                                FROM brain.ideal_stock WHERE as_of = :a AND scope <> 'Company' AND item_name IS NOT NULL
                                GROUP BY 1, 2"""), conn, params={"a": start.date()})
    base = base[base["branch"].isin(shops)]
    for c in ("held_start", "ideal_start", "annual", "held_12m"):
        base[c] = pd.to_numeric(base[c], errors="coerce").astype(float)

    # stock held each day since the start (daily ERP reports), counted as the ideal counts it (sets as one)
    dates = q.stock_dates(conn)
    days = sorted(d for d in pd.to_datetime(dates["snapshot_date"]) if d >= start)
    if not days:
        return pd.DataFrame(), start, None
    stock = bt.add_brick(sets.stock_units(q.brick_stock(conn, days)), rules)
    held = stock.groupby(["item_name", "branch", "snapshot_date"])["pcs"].sum().unstack("snapshot_date", fill_value=0)
    now = days[-1]
    held_now = held[now] if now in held else pd.Series(dtype=float)
    held_avg = held.mean(axis=1)

    sales = bt.add_brick(sets.sales_units(q.brick_sales(conn, (start + pd.Timedelta(days=1)).date())), rules)
    if rules.get("fc_exclude_orders", True):
        sales = sales[~sales["order_booked"].fillna(False).astype(bool)]
    sales = sales[(sales["sale_date"] > start) & (sales["sale_date"] <= now)]
    sold = sales.groupby(["item_name", "branch"])["pcs"].sum()

    key = pd.MultiIndex.from_frame(base[["item_name", "branch"]])
    base["held_now"] = held_now.reindex(key).fillna(0).to_numpy()
    base["held_avg"] = held_avg.reindex(key).fillna(0).to_numpy()
    base["sold"] = sold.reindex(key).fillna(0).to_numpy()
    base["days"] = (now - start).days
    ref = base["held_12m"].where(base["held_12m"] > 0, base["held_start"])
    resp = fc.held_response(base["held_avg"], ref.fillna(0), float(rules.get("fc_held_effect", 0) or 0))
    base["expected"] = base["annual"].fillna(0) * base["days"] / 365 * resp

    base["gap"] = base["ideal_start"] - base["held_start"]
    worth = ist.status(base["held_start"], base["ideal_start"], rules) != "keep"
    base["closed"] = np.where(worth & (base["gap"] != 0), (base["held_now"] - base["held_start"]) / base["gap"].where(base["gap"] != 0), np.nan)
    c = base["closed"]
    base["verdict"] = np.select([~worth, c >= 0.5, c >= MOVED, c > -MOVED],
                                ["no_gap", "followed", "partly", "not_yet"], "other_way")
    base["direction"] = np.where(~worth, "", np.where(base["gap"] > 0, "add", "cut"))
    return base, start, now


def summary(rows, by):
    """Products, gap and how much of it was closed, sold vs expected, per `by` (e.g. ["verdict"] or ["branch"])."""
    if rows.empty:
        return pd.DataFrame()
    r = rows.assign(gap_pcs=rows["gap"].abs().where(rows["verdict"] != "no_gap", 0),
                    closed_pcs=(np.sign(rows["gap"]) * (rows["held_now"] - rows["held_start"])).where(rows["verdict"] != "no_gap", 0))
    g = r.groupby(by).agg(products=("item_name", "size"), gap_pcs=("gap_pcs", "sum"), closed_pcs=("closed_pcs", "sum"),
                          sold=("sold", "sum"), expected=("expected", "sum")).reset_index()
    g["closed_share"] = np.where(g["gap_pcs"] > 0, g["closed_pcs"] / g["gap_pcs"].where(g["gap_pcs"] > 0), np.nan)
    g["sold_vs_expected"] = np.where(g["expected"] > 0, g["sold"] / g["expected"].where(g["expected"] > 0) - 1, np.nan)
    return g
