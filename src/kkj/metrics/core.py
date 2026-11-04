"""
Core metrics: pure functions on DataFrames (no database, no UI), one definition each.

Value basis: taxable_amount (pre-GST value) for both sales and stock, so every
ratio compares like with like. Rows without it (legacy files) fall back to
item_amt, which runs within ~0.4% of taxable_amount on current stock; the
fallback is flagged wherever it is used.

Industry definitions these follow (retail method, value at retail on both sides):
  stock turn        = net sales over a period / AVERAGE stock over the same period,
                      annualised. Standard practice is COGS / average stock at cost;
                      KKJ's ERP holds no cost, and the retail method (both sides at
                      selling value) gives the same ratio when margins are stable.
  days of inventory = 365 / annualised turn
  sell-through      = units sold / (units sold + units on hand at period end)
  weeks of cover    = units on hand / average weekly units sold (trailing window)
Average stock is time-weighted across the snapshots in the period (snapshot dates
are irregular), not the mean of the snapshots.
"""
import numpy as np
import pandas as pd

AGE_BUCKETS = [(0, 90, "0–90 d"), (91, 180, "91–180 d"), (181, 365, "181–365 d"),
               (366, 730, "1–2 yr"), (731, None, "2 yr +")]
PRICE_BANDS = [(0, 50_000, "< 50K"), (50_000, 100_000, "50K–1L"), (100_000, 300_000, "1L–3L"),
               (300_000, 500_000, "3L–5L"), (500_000, None, "5L +")]
CUSTOMER_GROUP = "Customer Diamond"          # customers' own items: never company stock


def value(df):
    """Row value on the common basis: taxable_amount, else item_amt."""
    tax = df["taxable_amount"] if "taxable_amount" in df else pd.Series(np.nan, index=df.index)
    return tax.where(tax.notna(), df["item_amt"]).astype(float)


def age_bucket(days):
    """Days -> ordered age bucket label (0–90 d ... 2 yr +)."""
    labels = [b[2] for b in AGE_BUCKETS]
    edges = [-np.inf] + [b[1] for b in AGE_BUCKETS[:-1]] + [np.inf]
    return pd.cut(pd.Series(days, dtype="float"), bins=edges, labels=labels, right=True)


def price_band(amount):
    """Value -> ordered price band (< 50K ... 5L +), lower bound inclusive."""
    labels = [b[2] for b in PRICE_BANDS]
    edges = [-np.inf] + [b[1] for b in PRICE_BANDS[:-1]] + [np.inf]
    return pd.cut(pd.Series(amount, dtype="float"), bins=edges, labels=labels, right=False)


def location_type(location):
    """Counter / approval / in transit / repair: where the stock physically is."""
    loc = pd.Series(location, dtype="object").fillna("").str.lower()
    return np.select([loc.str.contains("approval"), loc.str.contains("transit"),
                      loc.str.contains("repair"), loc.str.contains("counter|branch|mo diamond")],
                     ["Approval", "In transit", "Repair", "Counter"], default="Other")


def company_stock(stock):
    """Stock rows that are the company's (customers' own items excluded)."""
    return stock[stock["item_class_group"] != CUSTOMER_GROUP]


def net_sales(lines):
    """Sales lines with net_value / net_pcs; returns valued at the sale they reverse.

    A return has no taxable amount of its own. It is valued at minus the taxable
    amount of its original line (same bill + inventory_ref_code), and failing that
    at its own item_amt (flag value_fallback). Legacy rows use item_amt (flagged).
    """
    out = lines.copy()
    out["net_value"] = value(out)
    out["value_fallback"] = out["taxable_amount"].isna()
    out["net_pcs"] = out["pcs"].astype(float)
    is_return = out["pcs"] < 0
    if is_return.any() and "orig_bill_no" in out:
        originals = out[~is_return & out["taxable_amount"].notna()]
        originals = originals.drop_duplicates(["bill_no", "inventory_ref_code"])
        orig = originals.set_index(["bill_no", "inventory_ref_code"])[["taxable_amount", "pcs"]]
        idx = pd.MultiIndex.from_frame(out.loc[is_return, ["orig_bill_no", "inventory_ref_code"]])
        match = orig.reindex(idx).set_axis(out.index[is_return])
        found = match["taxable_amount"].notna() & (match["pcs"] > 0)
        rows = match.index[found]
        # a partial return (1 of 2 pcs) takes that share of the original line's value
        share = -out.loc[rows, "pcs"] / match.loc[rows, "pcs"]
        out.loc[rows, "net_value"] = -match.loc[rows, "taxable_amount"] * share
        out.loc[rows, "value_fallback"] = False
    return out


def time_weighted_average(series, start, end):
    """Average of a step-free series (dates -> values) over [start, end].

    Linear between snapshot dates, flat before the first and after the last
    snapshot inside the window. Returns NaN when no snapshot falls in the window.
    """
    s = pd.Series(series).sort_index()
    s.index = pd.to_datetime(s.index)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    inside = s[(s.index >= start) & (s.index <= end)]
    if inside.empty:
        return float("nan")
    points = inside.copy()
    points.loc[start] = inside.iloc[0] if start not in inside.index else inside.loc[start]
    points.loc[end] = inside.iloc[-1] if end not in inside.index else inside.loc[end]
    points = points.sort_index()
    days = (points.index - start).days.astype(float)
    total = days[-1]
    if total == 0:
        return float(points.iloc[0])
    return float(np.trapezoid(points.values, days) / total)


def stock_turn(net_sales_value, avg_stock_value, days):
    """Annualised stock turn: (net sales / average stock) x 365 / days."""
    if not avg_stock_value or days <= 0 or pd.isna(avg_stock_value):
        return float("nan")
    return float(net_sales_value) / float(avg_stock_value) * 365.0 / days


def days_of_inventory(turn):
    """Days of stock at the current selling rate: 365 / annualised turn."""
    return float("nan") if not turn or pd.isna(turn) else 365.0 / turn


def sell_through(units_sold, units_on_hand_end):
    """Share of the available units that sold: sold / (sold + on hand at period end)."""
    denominator = units_sold + units_on_hand_end
    return float("nan") if denominator <= 0 else units_sold / denominator


def weeks_of_cover(units_on_hand, units_sold, weeks):
    """Units on hand / average weekly units sold over the trailing `weeks`."""
    per_week = units_sold / weeks if weeks else 0
    return float("inf") if per_week <= 0 else units_on_hand / per_week
