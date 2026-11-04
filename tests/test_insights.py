"""Insight metrics on hand-checkable data: 2 snapshots, a 2-month period."""
import numpy as np
import pandas as pd
import pytest

from kkj.metrics import insights as ins

START, END = "2026-01-01", "2026-03-02"            # 60 days = 1.97 months


def hist(rows):
    return pd.DataFrame(rows, columns=["snapshot_date", "barcode", "supplier", "pcs", "value", "age"])


def test_sell_rate_by_age():
    # 10 new pieces and 10 two-year-old pieces held on both snapshots; 4 new + 1 old sold
    h = hist([(d, f"N{i}", "A", 1, 1e5, 20) for d in ("2026-01-01", "2026-03-01") for i in range(10)] +
             [(d, f"O{i}", "A", 1, 1e5, 800) for d in ("2026-01-01", "2026-03-01") for i in range(10)])
    s = pd.DataFrame({"pcs": [1] * 5, "value": [1e5] * 5, "age_at_sale": [15, 20, 25, 28, 790]})
    t = ins.sell_rate_by_age(h, s, START, END)
    months = ins.months_between(START, END)
    assert t.loc["0–30 d", "avg_pcs"] == 10
    assert t.loc["0–30 d", "monthly_sell_rate"] == pytest.approx(4 / months / 10)
    assert t.loc["2–3 yr", "monthly_sell_rate"] == pytest.approx(1 / months / 10)
    assert np.isnan(t.loc["91–180 d", "monthly_sell_rate"])           # nothing held there


def test_suggested_aged_days():
    by_age = pd.DataFrame({"monthly_sell_rate": [0.12, 0.09, 0.064, 0.045, 0.031, 0.024, 0.02, 0.005]},
                          index=ins.AGE_LABELS)
    # new = mean(0.12, 0.09) = 0.105; first band below half (0.0525) from the third band on is 181–365 d
    assert ins.suggested_aged_days(by_age) == 180


def test_supplier_performance_is_price_adjusted():
    # Two price bands: cheap sells 10%/month, dear sells 2%/month company-wide.
    # Supplier X holds only dear pieces and sells at 2%: performance 1.0, not "slow".
    rows = []
    for d in ("2026-01-01", "2026-03-01"):
        rows += [(d, f"XD{i}", "X", 1, 600_000, 100) for i in range(50)]
        rows += [(d, f"YC{i}", "Y", 1, 40_000, 100) for i in range(50)]
    h = hist(rows)
    months = ins.months_between(START, END)
    s = pd.DataFrame({"supplier": ["X"] * round(0.02 * 50 * months) + ["Y"] * round(0.10 * 50 * months),
                      "pcs": 1, "age_at_sale": 100})
    s["value"] = np.where(s["supplier"] == "X", 600_000, 40_000)
    t = ins.supplier_scorecard(h, s, START, END, bands=[0, 50_000, 500_000, np.inf], min_avg_pcs=10)
    assert t.loc["X", "performance"] == pytest.approx(1.0)
    assert t.loc["Y", "performance"] == pytest.approx(1.0)
    assert t.loc["X", "monthly_sell_rate"] < t.loc["Y", "monthly_sell_rate"]     # raw rate would mislead


def test_recovery_value_and_carrying_cost():
    stock = pd.DataFrame({"net_wt": [2.0], "rate": [9_000.0], "dia_amt": [50_000.0]})
    assert ins.recovery_value(stock, 0.7).iloc[0] == pytest.approx(18_000 + 35_000)
    assert ins.carrying_cost_per_month(12_000_000, 12) == pytest.approx(120_000)


def test_one_per_week_thins_daily_runs():
    dates = pd.to_datetime(["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-06-15"])
    h = pd.DataFrame({"snapshot_date": dates, "pcs": 1})
    kept = sorted(ins.one_per_week(h)["snapshot_date"].dt.strftime("%m-%d"))
    assert kept == ["06-15", "10-28", "11-02"]           # one per calendar week
