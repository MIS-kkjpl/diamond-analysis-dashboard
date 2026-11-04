"""
Is a stock snapshot really the stock of its date? Used for every past-date
snapshot (legacy CSVs and back-dated ERP reports) before it enters stock_hist.

  labeled_after:  labels whose label date is AFTER the snapshot date (a true
                  snapshot cannot hold them)
  sold_before:    labels sold in the SOLD_BEFORE_DAYS before the snapshot date
                  that are still in it (they should have left stock)
  signature:      hash of (barcode, item_amt), to spot identical copies
"""
import hashlib
from datetime import timedelta

import pandas as pd

from kkj.db import history as db

MAX_LABELED_AFTER_PCT = 0.5     # % of labels dated after the snapshot
MAX_SOLD_BEFORE_PCT = 5.0       # % of the previous days' sold labels still present
SOLD_BEFORE_DAYS = 30


def label_dates(df):
    """The label date the ERP ages from: Label Dt, falling back to Labeling Date."""
    return pd.to_datetime(df["label_dt"]).fillna(pd.to_datetime(df["labeling_date"]))


def integrity(df, snapshot_date, reported_age="age_days_reported"):
    """The numbers behind the checks (see module docstring) for one snapshot."""
    after = int((label_dates(df) > pd.Timestamp(snapshot_date)).sum())
    sold = db.sold_barcodes(snapshot_date - timedelta(days=SOLD_BEFORE_DAYS),
                            snapshot_date - timedelta(days=1))
    still = int(sold["barcode"].isin(df["barcode"]).sum())
    implied_export = (label_dates(df)
                      + pd.to_timedelta(df[reported_age].astype("float"), unit="D")).mode()
    signature = hashlib.md5(pd.util.hash_pandas_object(
        df[["barcode", "item_amt"]].sort_values("barcode").reset_index(drop=True), index=False).values
    ).hexdigest()
    return {
        "labels": len(df),
        "labeled_after": after,
        "labeled_after_pct": round(100 * after / max(len(df), 1), 2),
        "sold_prev_days": len(sold),
        "sold_before_still_present": still,
        "sold_before_pct": round(100 * still / max(len(sold), 1), 1),
        "implied_export_date": str(implied_export.iloc[0].date()) if len(implied_export) else None,
        "signature": signature,
    }


def reasons(i):
    """Why a snapshot fails, from its integrity() numbers ([] = it passes)."""
    out = []
    if i["labeled_after_pct"] > MAX_LABELED_AFTER_PCT:
        out.append(f"{i['labeled_after_pct']}% labels dated after the snapshot")
    if i["sold_before_pct"] > MAX_SOLD_BEFORE_PCT:
        out.append(f"{i['sold_before_pct']}% of the previous {SOLD_BEFORE_DAYS} days' sales still present")
    return out


def recomputed_age(df, snapshot_date):
    """Age on the snapshot date, the ERP's own definition (snapshot date - label date)."""
    return (pd.Timestamp(snapshot_date) - label_dates(df)).dt.days.astype("Int64")
