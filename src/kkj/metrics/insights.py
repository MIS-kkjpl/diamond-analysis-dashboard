"""
Insight metrics: how stock behaves over a period. Pure functions on DataFrames.

Inputs, at piece level:
  history: one row per piece per stock snapshot in the period
           (snapshot_date, barcode, item_name, item_class_group, supplier, branch, pcs, value, age)
  sales:   sale lines in the same period, returns excluded
           (sale_date, barcode, item_name, item_class_group, supplier, branch, pcs, value, age_at_sale)

"Monthly sell rate" = pieces sold per month / average pieces held, both measured on the
same slice (an age band, a supplier, a price band). It is the share of the slice that
sells in an average month: the sell-through rate used in retail, per month.
"""
import numpy as np
import pandas as pd

AGE_EDGES = [30, 90, 180, 365, 540, 730, 1095]
AGE_LABELS = ["0–30 d", "31–90 d", "91–180 d", "181–365 d", "1–1.5 yr", "1.5–2 yr", "2–3 yr", "3 yr +"]
DAYS_PER_MONTH = 30.44


def months_between(start, end):
    return max((pd.Timestamp(end) - pd.Timestamp(start)).days, 1) / DAYS_PER_MONTH


def age_band(days, edges=AGE_EDGES, labels=AGE_LABELS):
    return pd.cut(pd.Series(days, dtype="float"), [-np.inf] + list(edges) + [np.inf], labels=labels, right=True)


def one_per_week(history):
    """Keep the first snapshot of each week, so a run of daily snapshots doesn't outweigh
    weekly or fortnightly history when snapshots are averaged."""
    dates = pd.Series(sorted(history["snapshot_date"].unique()))
    keep = dates.groupby(dates.dt.to_period("W")).min()
    return history[history["snapshot_date"].isin(keep)]


def _rate_table(history, sales, key_hist, key_sales, n_snapshots, months):
    """Average held vs sold per month for one slicing key."""
    held = history.groupby(key_hist, observed=False).agg(avg_pcs=("pcs", "sum"), avg_value=("value", "sum")) / n_snapshots
    sold = sales.groupby(key_sales, observed=False).agg(sold_pcs=("pcs", "sum"), sold_value=("value", "sum"))
    t = held.join(sold, how="outer").fillna(0)
    t["sold_per_month"] = t["sold_pcs"] / months
    t["monthly_sell_rate"] = np.where(t["avg_pcs"] > 0, t["sold_per_month"] / t["avg_pcs"], np.nan)
    t["months_of_stock"] = np.where(t["sold_per_month"] > 0, t["avg_pcs"] / t["sold_per_month"], np.inf)
    t["annual_turn"] = np.where(t["avg_value"] > 0, t["sold_value"] / months * 12 / t["avg_value"], np.nan)
    return t


def sell_rate_by_age(history, sales, start, end):
    """How fast pieces sell at each age: monthly sell rate and months for half of them to sell."""
    n = history["snapshot_date"].nunique()
    h = history.assign(band=age_band(history["age"]).values)
    s = sales.assign(band=age_band(sales["age_at_sale"]).values)
    t = _rate_table(h, s, "band", "band", n, months_between(start, end))
    t["months_to_sell_half"] = np.where(t["monthly_sell_rate"] > 0,
                                        np.log(2) / -np.log1p(-t["monthly_sell_rate"].clip(upper=0.999)), np.inf)
    return t


def suggested_aged_days(by_age, share=0.5):
    """First age band edge where the sell rate falls below `share` of the newest pieces' rate.

    Bands under 90 days are the reference ("new"). Returns the band's lower edge in days, or None.
    """
    rates = by_age["monthly_sell_rate"].dropna()
    if len(rates) < 3:
        return None
    new_rate = by_age["monthly_sell_rate"].iloc[:2].mean()
    lower_edges = [0] + AGE_EDGES
    for i, rate in enumerate(by_age["monthly_sell_rate"]):
        if i >= 2 and pd.notna(rate) and rate < share * new_rate:
            return lower_edges[i]
    return None


def supplier_scorecard(history, sales, start, end, bands, min_avg_pcs=40):
    """Suppliers' sell rate, adjusted for the price bands they sell in.

    expected_sold = each supplier's average pieces held in every price band x that band's
    company-wide monthly sell rate. performance = actual / expected: 1.0 = sells like the
    company does at those prices, 2.0 = twice as fast, 0.5 = half.
    """
    months = months_between(start, end)
    n = history["snapshot_date"].nunique()
    h = history.assign(band=pd.cut(history["value"], bands).values)
    s = sales.assign(band=pd.cut(sales["value"], bands).values)
    band_rate = _rate_table(h, s, "band", "band", n, months)["monthly_sell_rate"].fillna(0)
    held = h.groupby(["supplier", "band"], observed=False)["pcs"].sum().div(n).rename("avg_pcs").reset_index()
    held["expected_per_month"] = held["avg_pcs"] * held["band"].map(band_rate).astype(float)
    expected = held.groupby("supplier")["expected_per_month"].sum()
    t = _rate_table(h, s, "supplier", "supplier", n, months)
    t["expected_per_month"] = expected.reindex(t.index).fillna(0)
    t["performance"] = np.where(t["expected_per_month"] > 0, t["sold_per_month"] / t["expected_per_month"], np.nan)
    last = history[history["snapshot_date"] == history["snapshot_date"].max()]
    t["aged_share_now"] = last.assign(old=last["age"] > 365).groupby("supplier")["old"].mean().reindex(t.index)
    t["median_days_to_sell"] = sales.groupby("supplier")["age_at_sale"].median().reindex(t.index)
    return t[t["avg_pcs"] >= min_avg_pcs].sort_values("performance")


def assortment_gap(history, sales, start, end, key):
    """Share of stock vs share of sales per slice (price band, item, ...), with months of stock."""
    t = _rate_table(history, sales, key, key, history["snapshot_date"].nunique(), months_between(start, end))
    t["share_of_stock"] = t["avg_pcs"] / t["avg_pcs"].sum()
    t["share_of_sales"] = t["sold_pcs"] / t["sold_pcs"].sum() if t["sold_pcs"].sum() else 0
    t["gap"] = t["share_of_sales"] - t["share_of_stock"]          # > 0: under-held for what sells
    return t


def channel_comparison(sales, channel):
    """Age and discount of what each sales channel sells (channel: Series aligned to sales)."""
    s = sales.assign(channel=channel.values)
    out = s.groupby("channel").agg(lines=("pcs", "size"), value=("value", "sum"),
                                   median_age_at_sale=("age_at_sale", "median"),
                                   share_over_1y=("age_at_sale", lambda x: (x > 365).mean()))
    if "discount" in s:
        out["median_discount"] = s.groupby("channel")["discount"].median()
    return out


def recovery_value(stock, diamond_recovery_share):
    """What a piece would fetch if broken up: metal (net_wt x the piece's own per-gram rate) plus
    a share of its diamond value. A floor for liquidation decisions, not a valuation."""
    metal = stock["net_wt"].fillna(0) * stock["rate"].fillna(0)
    return metal + stock["dia_amt"].fillna(0) * diamond_recovery_share


def carrying_cost_per_month(value, annual_cost_pct):
    """Cost of money tied up in stock, per month."""
    return value * annual_cost_pct / 100 / 12
