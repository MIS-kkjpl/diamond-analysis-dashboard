"""Seasonality (kkj.metrics.seasonality) and the festival recommendation on hand-checkable data."""
import pandas as pd
import pytest

from kkj import rules as R
from kkj.brain import generators as gen
from kkj.metrics import seasonality as sn

RULES = {r.key: r.default for r in R.RULES}


def _year(fy, per_month, key="D RING"):
    """Sales lines for one financial year: per_month[i] pieces in the i-th month from April, on the 15th."""
    rows = []
    for i, n in enumerate(per_month):
        month = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3][i]
        year = fy if month >= 4 else fy + 1
        rows.append({"sale_date": pd.Timestamp(year, month, 15), "item_name": key, "pcs": n})
    # first and last week of the year, so it counts as complete
    rows += [{"sale_date": pd.Timestamp(fy, 4, 1), "item_name": key, "pcs": 0},
             {"sale_date": pd.Timestamp(fy + 1, 3, 31), "item_name": key, "pcs": 0}]
    return rows


def test_monthly_index_averages_each_years_shares():
    flat = [10] * 12
    peak = [10] * 6 + [40] + [10] * 5                   # October four times a normal month
    daily = pd.DataFrame(_year(2022, flat) + _year(2023, peak))
    idx = sn.monthly_index(daily, "item_name", min_year_pcs=50).set_index("item_name").loc["D RING"]
    assert idx["years"] == 2
    assert idx["index_10"] == pytest.approx(12 * (10 / 120 + 40 / 150) / 2)
    assert sum(idx[f"index_{m}"] for m in sn.MONTHS) == pytest.approx(12)


def test_partial_years_are_left_out():
    daily = pd.DataFrame(_year(2022, [10] * 12) + [{"sale_date": pd.Timestamp(2023, 5, 1), "item_name": "D RING", "pcs": 99}])
    assert sn.complete_years(daily) == [2022]


def test_window_share_and_turn_annualisation():
    row = {f"index_{m}": 1.0 for m in sn.MONTHS}
    assert sn.window_share(row, "2026-01-01", "2026-12-31") == pytest.approx(1.0)
    row[f"index_10"], row["index_4"] = 3.0, 0.0                       # October heavy, April empty
    oct_share = sn.window_share(row, "2026-10-01", "2026-10-31")
    assert oct_share == pytest.approx(3 / 12)
    # 30 pieces sold in October = 120 a year once its season is known, not 30 × 12 = 360
    assert 30 / oct_share == pytest.approx(120)


def test_festival_lift_aligns_to_each_years_date():
    rows = []
    for fy, day in ((2022, pd.Timestamp(2022, 10, 23)), (2023, pd.Timestamp(2023, 11, 10))):
        rows += [{"sale_date": pd.Timestamp(fy, 4, 1) + pd.Timedelta(days=d), "item_name": "D TOPS", "pcs": 1}
                 for d in range(365)]                                 # 1 a day all year...
        rows += [{"sale_date": day - pd.Timedelta(days=d), "item_name": "D TOPS", "pcs": 4} for d in range(22)]
    daily = pd.DataFrame(rows)                                        # ...plus 4 more a day in the 3 weeks before
    fests = pd.DataFrame({"name": ["Dhanteras", "Dhanteras"], "day": [pd.Timestamp(2022, 10, 23), pd.Timestamp(2023, 11, 10)]})
    lift = sn.festival_lift(daily, fests, "item_name", before=21, after=3, min_year_pcs=50).iloc[0]
    assert lift["years"] == 2 and lift["window_pcs"] == pytest.approx(25 + 4 * 22)
    assert lift["lift"] == pytest.approx((113 / 25) / (453 / 365))      # 4.5 a day vs 1.24 over the year


def test_festival_stock_card_when_short_and_one_card_for_dhanteras_and_diwali():
    base = {"item_name": "D TOPS", "group": "Diamond Jewellery", "lift": 2.5, "years": 4, "growth": 1.1,
            "held_pcs": 60, "held_value": 60 * 150_000}
    outlook = pd.DataFrame([
        {**base, "festival": "Dhanteras", "day": pd.Timestamp(2027, 10, 27), "days_away": 40, "past_window_pcs": 60,
         "expected_pcs": 66},
        {**base, "festival": "Diwali", "day": pd.Timestamp(2027, 10, 29), "days_away": 42, "past_window_pcs": 50,
         "expected_pcs": 55},
        {**base, "festival": "Gudi Padwa", "day": pd.Timestamp(2027, 4, 7), "days_away": 3, "past_window_pcs": 90,
         "expected_pcs": 99}])                                        # too close to act on
    acts = gen.festival_stock(outlook, {**RULES, "festival_stock_cover": 2.0})
    assert len(acts) == 1 and acts[0]["title"].startswith("Stock up D TOPS for Dhanteras")
    assert acts[0]["evidence"]["target (pcs)"] == 132 and "+72 pcs" in acts[0]["impact_label"]
    assert gen.festival_stock(outlook.assign(held_pcs=200), RULES) == []      # enough held: no card
