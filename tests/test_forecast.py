"""Demand forecast (kkj.metrics.forecast) and ideal stock (kkj.metrics.ideal_stock) on small made-up data."""
import numpy as np
import pandas as pd
import pytest

from kkj import rules as R
from kkj.metrics import forecast as fc
from kkj.metrics import ideal_stock as ist

RULES = {r.key: r.default for r in R.RULES}


def make(sales_per_month, months=24, group="18KT · D RING · 0.25–0.5ct", branch="KJ2", item="D RING", stocked=True):
    """Stock and sales rows for one brick group at one branch: a fixed number sold every month."""
    end = pd.Period("2026-10", "M")
    periods = pd.period_range(end - months + 1, end, freq="M")
    sales = pd.DataFrame([{"sale_date": p.start_time + pd.Timedelta(days=10), "branch": branch, "brick_group": group,
                           "item_class_group": "Diamond Jewellery", "purity": "18KT", "item_name": item,
                           "pcs": 1.0, "order_booked": False, "value": 100_000}
                          for p in periods for _ in range(sales_per_month)])
    stock = pd.DataFrame([{"snapshot_date": p.end_time.normalize(), "branch": branch, "brick_group": group,
                           "item_class_group": "Diamond Jewellery", "purity": "18KT", "item_name": item,
                           "pcs": 5.0 if stocked else 0.0} for p in periods])
    return stock, sales, end


def run(stock, sales, end, **over):
    rules = {**RULES, "fc_trend": False, "fc_seasonal": False, **over}
    p, attrs = fc.panel(stock, sales, rules, end)
    return fc.forecast_at(p, attrs, end, rules, fc.season_tables(sales, rules)), p, attrs, rules


def test_steady_seller_forecasts_its_rate():
    stock, sales, end = make(2)
    f, *_ = run(stock, sales, end)
    r = f.loc[("18KT · D RING · 0.25–0.5ct", "KJ2")]
    assert r["rate"] == pytest.approx(2.0, rel=0.02) and r["annual"] == pytest.approx(24.0, rel=0.02)


def test_order_booked_sales_are_not_demand():
    stock, sales, end = make(2)
    sales.loc[sales.index % 2 == 0, "order_booked"] = True          # half were customer orders
    f, *_ = run(stock, sales, end)
    assert f["rate"].iloc[0] == pytest.approx(1.0, rel=0.05)
    f2, *_ = run(stock, sales, end, fc_exclude_orders=False)
    assert f2["rate"].iloc[0] == pytest.approx(2.0, rel=0.05)


def test_a_new_group_borrows_from_its_product_and_a_dead_one_is_not_propped_up():
    s1, sa1, end = make(4, group="18KT · D RING · 0.25–0.5ct")
    s2, _, _ = make(0, group="18KT · D RING · 0.5–1ct")                 # stocked two years, never sold
    s3, sa3, _ = make(1, months=1, group="18KT · D RING · 1–2ct")       # one month old, sold 1
    stock, sales = pd.concat([s1, s2, s3]), pd.concat([sa1, sa3])
    f, *_ = run(stock, sales, end)
    dead = f.loc[("18KT · D RING · 0.5–1ct", "KJ2"), "rate"]
    new = f.loc[("18KT · D RING · 1–2ct", "KJ2"), "rate"]
    assert dead < 0.1                                                   # 24 months of zero sales are evidence
    assert 0 < new < 4                                                  # leans on the product, not just its one sale
    total = f["rate"].sum()
    assert total == pytest.approx(4 + 1 / 24, rel=0.25)                 # borrowing does not invent demand


def test_backtest_and_accuracy_beat_nothing_on_a_flat_series():
    stock, sales, end = make(3, months=36)
    _, p, attrs, rules = run(stock, sales, end)
    bt = fc.backtest(p, attrs, sales, rules, [end - 6, end - 4, end - 2], horizon=2)
    a = fc.accuracy(bt, [])
    assert a["wape"].iloc[0] == pytest.approx(0, abs=0.02) and a["bias"].iloc[0] == pytest.approx(0, abs=0.02)


def test_count_quantile_matches_the_poisson_table():
    assert ist.count_quantile(2, 1.0, 0.95) == 5           # Poisson(2): P(X<=4)=0.947, P(X<=5)=0.983
    assert ist.count_quantile(0, 1.0, 0.95) == 0
    assert ist.count_quantile(2, 3.0, 0.95) > 5            # lumpier demand needs more cover


def test_split_keeps_the_total_and_follows_the_weights():
    assert list(ist.split(10, [1, 1, 2])) == [2.0, 3.0, 5.0] or ist.split(10, [1, 1, 2]).sum() == 10
    assert ist.split(10, [1, 1, 2]).sum() == 10 and ist.split(10, [1, 1, 2])[2] == 5
    assert list(ist.split(3, [0, 0, 0])) == [1.0, 1.0, 1.0]


def test_a_product_override_at_all_branches_is_split_and_a_group_override_wins():
    rows = pd.DataFrame({"brick_group": ["A", "B", "A", "B"], "item_name": ["D RING"] * 4,
                         "branch": ["KJ2", "KJ2", "KJ3", "KJ3"], "ideal_pcs": [6.0, 2.0, 2.0, 0.0],
                         "ideal_from": ["choice"] * 4})
    o = pd.DataFrame([{"level": "product", "key": "D RING", "branch": "*", "pcs": 20, "reason": "bridal"},
                      {"level": "group", "key": "B", "branch": "KJ3", "pcs": 3, "reason": "new counter"}])
    ist.apply_overrides(rows, o)
    assert rows.loc[:2, "ideal_pcs"].tolist() == [12.0, 4.0, 4.0]       # 20 split 6:2:2 (the 0 row gets none)
    assert rows.loc[3, "ideal_pcs"] == 3 and rows.loc[3, "override_reason"] == "new counter"
    assert (rows["ideal_from"] == "override").all()


def test_status_needs_both_the_band_and_the_smallest_change():
    rules = {**RULES, "ideal_band_pct": 20.0, "ideal_min_change_pcs": 2}
    s = ist.status([10, 10, 10, 13, 3], [10, 13, 7, 10, 0], rules)
    assert list(s) == ["keep", "increase", "reduce", "reduce", "phase_out"]


def test_ideal_is_the_largest_of_demand_choice_and_display_minimum():
    stock, sales, end = make(2)
    f, *_ = run(stock, sales, end)
    current = pd.Series({("18KT · D RING · 0.25–0.5ct", "KJ2"): 10.0})
    current.index = pd.MultiIndex.from_tuples(current.index, names=fc.BASE)
    rules = {**RULES, "choice_months": 18}
    rows = ist.compute(f, current, pd.Series({"18KT · D RING · 0.25–0.5ct": 1e6}), {("18KT · D RING · 0.25–0.5ct", "KJ2")},
                       rules, pd.DataFrame(columns=["param", "level", "key", "value"]), pd.DataFrame(), ({}, 1.0))
    r = rows.iloc[0]
    assert r["for_choice"] == 36 and r["ideal_pcs"] == 36 and r["ideal_from"] == "choice"   # 18 months × 2 a month
    assert r["grade"] == "A" and r["status"] == "increase"
    params = pd.DataFrame([{"param": "choice_months", "level": "product", "key": "D RING", "value": "3"}])
    r2 = ist.compute(f, current, pd.Series({"18KT · D RING · 0.25–0.5ct": 1e6}), set(), rules, params, pd.DataFrame(),
                     ({}, 1.0)).iloc[0]
    assert r2["for_choice"] == 6 and r2["ideal_pcs"] == max(6, r2["for_demand"])            # product override applies


def test_a_fixed_ideal_due_for_review_comes_back_with_what_sold_since():
    from kkj.brain import generators as gen
    as_of = pd.Timestamp("2026-11-09")
    branch = pd.DataFrame({"brick_group": ["G1", "G1"], "item_name": ["D NOSE PIN"] * 2, "branch": ["KJ2", "KJ3"],
                           "calc_pcs": [30.0, 10.0], "ideal_pcs": [12.0, 4.0], "current_pcs": [20.0, 6.0],
                           "annual": [24.0, 8.0]})
    demand = pd.DataFrame({"brick_group": ["G1"] * 5, "item_name": ["D NOSE PIN"] * 5, "branch": ["KJ2"] * 5,
                           "sale_date": pd.to_datetime(["2026-08-20", "2026-09-10", "2026-10-01", "2026-10-15", "2026-07-01"]),
                           "pcs": [1.0] * 5})
    turns = pd.DataFrame({"level": ["group"], "scope": ["Company"], "brick": ["G1"], "avg_piece_value": [20_000.0]})
    o = pd.DataFrame([{"level": "product", "key": "D NOSE PIN", "branch": "*", "pcs": 16, "reason": "simple design",
                       "updated_by": "Director", "updated_at": pd.Timestamp("2026-08-11", tz="Asia/Kolkata"),
                       "review_on": pd.Timestamp("2026-11-09").date(), "calc_at_fix": 40, "annual_at_fix": 32,
                       "held_at_fix": 30},
                      {"level": "group", "key": "G1", "branch": "KJ3", "pcs": 4, "reason": "not due yet",
                       "updated_by": "x", "updated_at": pd.Timestamp("2026-11-01", tz="Asia/Kolkata"),
                       "review_on": pd.Timestamp("2027-01-30").date(), "calc_at_fix": 10, "annual_at_fix": 8,
                       "held_at_fix": 6}])
    acts = gen.ideal_reviews({"branch": branch, "demand": demand, "as_of": as_of}, o, turns, RULES)
    assert len(acts) == 1                                   # only the one whose review date has come
    a = acts[0]
    assert a["kind"] == "ideal_review" and "D NOSE PIN" in a["title"] and "all branches" in a["title"]
    assert "sold 4" in a["summary"] and "simple design" in a["summary"]       # 4 sales after the fix date
    assert a["evidence"]["calculated now"] == "40" and a["subject"]["fix_branch"] == "*"


def test_fixes_pointing_the_same_way_suggest_a_rule_change():
    from kkj.brain import fixes
    latest = pd.DataFrame({"scope": ["KJ2", "KJ3", "KJ7"], "brick_group": ["G1", "G2", "G3"],
                           "item_name": ["D NOSE PIN"] * 3, "calc_pcs": [30.0, 20.0, 10.0], "ideal_pcs": [9.0, 6.0, 10.0]})
    o = pd.DataFrame([{"level": "group", "key": "G1", "branch": "KJ2", "pcs": 9, "reason": "simple design"},
                      {"level": "group", "key": "G2", "branch": "KJ3", "pcs": 6, "reason": "simple design"}])
    s = fixes.suggestions(o, latest, 18)
    assert len(s) == 1 and s[0]["product"] == "D NOSE PIN" and s[0]["months"] == pytest.approx(5.4)
    assert fixes.suggestions(o.iloc[:1], latest, 18) == []          # one fix is not a pattern
