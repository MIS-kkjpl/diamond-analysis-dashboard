"""
Seasonality from five years of sales (sales.hist_line), in pieces.

  monthly index   12 × the average share of a financial year's pieces sold in each month, over the
                  complete financial years (April-March) where the key sold enough to judge.
                  1.0 = an average month; 2.0 = twice an average month.
  festival lift   the pieces sold in a window around a festival (default 21 days before to 3 after,
                  aligned to that year's date, since festivals move between months), per day, against
                  that year's average day; and the window's share of the year.
  window share    the share of a typical year's pieces that falls between two dates, from the monthly
                  index. Used to annualise a stock-turn window by its season instead of by its length
                  (a 3-month window over Diwali holds far more than a quarter of the year's sales).

Keys are an item group (Gold, Diamond Jewellery, ...) or a product (D RING); a product without enough
history falls back to its item group, and that to no adjustment.
"""
import calendar

import numpy as np
import pandas as pd

MONTHS = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]            # financial-year order


def fy_start(dates):
    d = pd.to_datetime(pd.Series(dates))
    return np.where(d.dt.month >= 4, d.dt.year, d.dt.year - 1)


def complete_years(daily):
    """Financial years with sales data from the first week of April to the last week of March."""
    d = pd.to_datetime(daily["sale_date"])
    by = pd.DataFrame({"fy": fy_start(d), "d": d}).groupby("fy")["d"].agg(["min", "max"])
    return [int(fy) for fy, r in by.iterrows()
            if r["min"] <= pd.Timestamp(fy, 4, 7) and r["max"] >= pd.Timestamp(fy + 1, 3, 24)]


def monthly_index(daily, key, min_year_pcs=100):
    """One row per key: index_4 .. index_3 (April .. March), years used, average pieces a year.

    daily: sale_date, <key>, pcs (sales lines or daily totals; returns may be included).
    """
    years = complete_years(daily)
    d = daily[np.isin(fy_start(daily["sale_date"]), years)].copy()
    d["fy"], d["month"] = fy_start(d["sale_date"]), pd.to_datetime(d["sale_date"]).dt.month
    by = d.groupby([key, "fy", "month"])["pcs"].sum().unstack("month", fill_value=0).reindex(columns=MONTHS, fill_value=0)
    total = by.sum(axis=1)
    by = by[total >= min_year_pcs]
    share = by.div(by.sum(axis=1), axis=0)
    out = (12 * share.groupby(level=key).mean()).rename(columns=lambda m: f"index_{m}")
    out["years"] = share.groupby(level=key).size()
    out["avg_year_pcs"] = by.sum(axis=1).groupby(level=key).mean()
    return out.reset_index()


def festival_lift(daily, festivals, key, before=21, after=3, min_year_pcs=100):
    """One row per (festival, key): lift (window per day ÷ the year's average day), share of the year in the
    window, pieces in the window on average, occurrences used."""
    years = set(complete_years(daily))
    d = daily.assign(sale_date=pd.to_datetime(daily["sale_date"]))
    d["fy"] = fy_start(d["sale_date"])
    year_pcs = d.groupby([key, "fy"])["pcs"].sum()
    rows = []
    for f in festivals.itertuples():
        day = pd.Timestamp(f.day)
        fy = int(fy_start([day])[0])
        if fy not in years:
            continue
        lo, hi = day - pd.Timedelta(days=before), day + pd.Timedelta(days=after)
        win = d[(d["sale_date"] >= lo) & (d["sale_date"] <= hi)].groupby(key)["pcs"].sum()
        for k, pcs in win.items():
            yp = year_pcs.get((k, fy), 0)
            if yp < min_year_pcs:
                continue
            rows.append({"festival": f.name, key: k, "fy": fy, "window_pcs": pcs, "share": pcs / yp,
                         "lift": (pcs / (before + after + 1)) / (yp / 365)})
    if not rows:
        return pd.DataFrame(columns=["festival", key, "lift", "share", "window_pcs", "years"])
    r = pd.DataFrame(rows)
    return (r.groupby(["festival", key]).agg(lift=("lift", "mean"), share=("share", "mean"),
                                             window_pcs=("window_pcs", "mean"), years=("fy", "nunique"))
            .reset_index())


def window_share(index_row, start, end):
    """Share of a typical year's pieces sold from start to end (inclusive), from one key's monthly index."""
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    if len(days) == 0:
        return 0.0
    per_day = [index_row[f"index_{d.month}"] / 12 / calendar.monthrange(d.year, d.month)[1] for d in days]
    return float(np.sum(per_day))


def shares_for_windows(index, key, as_of, windows):
    """{key value: {window name: share of the year}} for windows ending on as_of, e.g. {"3m": 84}."""
    as_of = pd.Timestamp(as_of)
    out = {}
    for r in index.to_dict("records"):
        out[r[key]] = {name: window_share(r, as_of - pd.Timedelta(days=days - 1), as_of) for name, days in windows.items()}
    return out


def expected_pcs(annual_pcs, index_row, start, end):
    """Pieces expected between two dates for a key selling annual_pcs a year."""
    return annual_pcs * window_share(index_row, start, end)
