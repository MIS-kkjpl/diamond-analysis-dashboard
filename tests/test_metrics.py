"""Metric definitions on hand-checkable examples."""
import math

import pandas as pd
import pytest

from kkj.metrics import core as m


def test_value_prefers_taxable_amount():
    df = pd.DataFrame({"taxable_amount": [93.0, None], "item_amt": [100.0, 50.0]})
    assert m.value(df).tolist() == [93.0, 50.0]


def test_age_buckets_edges():
    assert m.age_bucket([0, 90, 91, 365, 366, 730, 731]).astype(str).tolist() == [
        "0–90 d", "0–90 d", "91–180 d", "181–365 d", "1–2 yr", "1–2 yr", "2 yr +"]


def test_price_band_lower_bound_inclusive():
    assert m.price_band([49_999, 50_000, 299_999, 500_000]).astype(str).tolist() == [
        "< 50K", "50K–1L", "1L–3L", "5L +"]


def test_location_type():
    assert list(m.location_type(["KJ2 F Counter", "HO Approval", "KJ4 InTransit", "HO Repair",
                                 "Supply Counter", "Mumbai Branch", None])) == [
        "Counter", "Approval", "In transit", "Repair", "Counter", "Counter", "Other"]


def test_net_sales_values_return_at_original_taxable():
    lines = pd.DataFrame({
        "bill_no": ["MK2SB-1", "MK2SR-9", "LEG-1"],
        "inventory_ref_code": [7, 7, None],
        "orig_bill_no": ["MK2SB-1", "MK2SB-1", None],
        "pcs": [1.0, -1.0, 1.0],
        "item_amt": [100.0, -101.0, 40.0],
        "taxable_amount": [93.0, None, None],
    })
    out = m.net_sales(lines)
    assert out["net_value"].tolist() == [93.0, -93.0, 40.0]
    assert out["value_fallback"].tolist() == [False, False, True]
    assert out["net_value"].sum() == 40.0          # the sale and its return cancel


def test_partial_return_takes_its_share():
    lines = pd.DataFrame({"bill_no": ["B1", "R1"], "inventory_ref_code": [5, 5], "orig_bill_no": ["B1", "B1"],
                          "pcs": [2.0, -1.0], "item_amt": [200.0, -100.0], "taxable_amount": [180.0, None]})
    assert m.net_sales(lines)["net_value"].tolist() == [180.0, -90.0]


def test_time_weighted_average():
    s = pd.Series({"2026-04-01": 100.0, "2026-04-11": 200.0})
    # linear 100 -> 200 over 10 days, flat 200 for the next 10: (150*10 + 200*10) / 20
    assert m.time_weighted_average(s, "2026-04-01", "2026-04-21") == pytest.approx(175.0)
    assert math.isnan(m.time_weighted_average(s, "2026-05-01", "2026-05-31"))


def test_turn_doi_sell_through_cover():
    turn = m.stock_turn(net_sales_value=50, avg_stock_value=100, days=182.5)   # half a year
    assert turn == pytest.approx(1.0)
    assert m.days_of_inventory(turn) == pytest.approx(365.0)
    assert m.sell_through(30, 70) == pytest.approx(0.30)
    assert m.weeks_of_cover(units_on_hand=120, units_sold=130, weeks=13) == pytest.approx(12.0)
    assert m.weeks_of_cover(5, 0, 13) == float("inf")
