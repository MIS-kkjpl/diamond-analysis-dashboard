"""Legacy import helpers: date formats, snapshot file names, barcode rebuild rules."""
import datetime as dt
from pathlib import Path

import pandas as pd

from kkj.pipelines.legacy_import import (barcode_rules, rebuild_barcodes, snapshot_date_from_name,
                                         to_dates)


def test_to_dates_reads_both_sales_formats():
    out = to_dates(pd.Series(["05-Jan-26", "29/05/2026", "garbage", None]))
    assert out.iloc[0] == pd.Timestamp("2026-01-05")
    assert out.iloc[1] == pd.Timestamp("2026-05-29")
    assert out.iloc[2:].isna().all()


def test_snapshot_date_accepts_single_digit_day():
    assert snapshot_date_from_name(Path("1_12_2025_Stock.csv")) == dt.date(2025, 12, 1)
    assert snapshot_date_from_name(Path("30_06_2026_Stock.csv")) == dt.date(2026, 6, 30)


def test_barcode_rules_learn_prefix_and_padding():
    known = pd.DataFrame({
        "item_name": ["D TOPS", "D TOPS", "D Kundan Tops", "D KUNDAN SET FIX"],
        "barcode":   ["DTP15180", "DTP998", "DKT0002", "124-A2437"],   # last one: not PREFIX+label
        "label_no":  [15180, 998, 2, 227],
    })
    rules = barcode_rules(known)
    assert rules == {"D TOPS": ("DTP", 0), "D Kundan Tops": ("DKT", 4)}

    sales = pd.DataFrame({"item_name": ["D TOPS", "D Kundan Tops", "UNKNOWN ITEM", "D TOPS"],
                          "label_no": pd.array([16179, 9, 5, None], dtype="Int64")})
    out = rebuild_barcodes(sales, rules)
    assert out.iloc[:2].tolist() == ["DTP16179", "DKT0009"]
    assert out.iloc[2:].isna().all()       # no rule / no label no -> missing
