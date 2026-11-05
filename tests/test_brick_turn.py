"""Weighted stock turn per brick (kkj.metrics.brick_turn) on small hand-checkable data."""
import numpy as np
import pandas as pd
import pytest

from kkj import rules as R
from kkj.metrics import brick_turn as bt

RULES = {r.key: r.default for r in R.RULES}
AS_OF = pd.Timestamp("2026-11-04")
DATES = [AS_OF - pd.Timedelta(days=14 * k) for k in range(26)]       # one snapshot a fortnight for a year


def test_karat_and_weight_band():
    assert bt.karat([58.3, 75, 91.6, 0, None]).tolist() == ["14KT", "18KT", "22KT", "", ""]
    labels, order = bt.weight_band([0.05, 0.3, -0.3, 7, 0, None], [0.1, 0.25, 0.5, 1, 2, 5])
    assert labels.tolist() == ["< 0.1ct", "0.25–0.5ct", "0.25–0.5ct", "5ct +", bt.NO_WEIGHT, bt.NO_WEIGHT]
    assert order.tolist() == [0, 2, 2, 6, -1, -1]                             # returns carry negative weights


def test_brick_key_and_loose_diamond_carats_from_gross_weight():
    df = pd.DataFrame({"purity": [75.0, 0.0], "item_name": ["D RING", "LOOSE DIAMOND"], "category": ["MICRO", None],
                       "sub_category": [None, None], "dia_wt": [0.3, 0.0], "gross_wt": [4.2, 1.05]})
    b = bt.add_brick(df, RULES)
    assert b["brick"].tolist() == ["18KT · D RING · MICRO · 0.25–0.5ct", "LOOSE DIAMOND · 1–2ct"]
    g = bt.add_brick(df, {**RULES, "turn_weight_basis": "gross_wt", "turn_weight_edges": [2, 5]})
    assert g["weight_band"].tolist() == ["2–5g", "< 2g"]


def test_grid_takes_the_latest_snapshot_in_each_fortnight_and_leaves_gaps():
    grid = bt.fortnight_grid(AS_OF, [AS_OF, AS_OF - pd.Timedelta(days=3), AS_OF - pd.Timedelta(days=30)], periods=4)
    assert [s for _, s in grid] == [AS_OF, None, AS_OF - pd.Timedelta(days=30), None]


def _stock(pcs_by_date, brick="b", band=2, group="Diamond Jewellery", value_each=100_000):
    return pd.DataFrame([{"snapshot_date": d, "brick": brick, "item_class_group": group, "purity": "18KT",
                          "item_name": "D RING", "category": "MICRO", "sub_category": "", "weight_band": f"band{band}",
                          "band_order": band, "pcs": p, "value": p * value_each}
                         for d, p in pcs_by_date.items()])


def _sales(dates_pcs, brick="b", band=2):
    return pd.DataFrame([{"sale_date": d, "brick": brick, "item_class_group": "Diamond Jewellery", "purity": "18KT",
                          "item_name": "D RING", "category": "MICRO", "sub_category": "", "weight_band": f"band{band}",
                          "band_order": band, "pcs": p, "value": p * 100_000} for d, p in dates_pcs])


def _one(stock, sales, **rules):
    t = bt.compute(stock, sales, AS_OF, {**RULES, **rules})
    return t.set_index("brick").loc["b"]


def test_all_windows_qualify_blend_60_30_10_and_reduce():
    stock = _stock({d: 10 for d in DATES})                                    # 10 pcs all year
    sales = _sales([(AS_OF - pd.Timedelta(days=7 * k), 1) for k in range(52)])  # 1 a week = 52 a year
    r = _one(stock, sales, target_turn=8)
    assert r["pattern"] == "3m+6m+12m" and r["weights"] == "60/30/10"
    assert r["turn_12m"] == pytest.approx(52 / 10, rel=0.03)
    assert r["blended_turn"] == pytest.approx(0.6 * r["turn_3m"] + 0.3 * r["turn_6m"] + 0.1 * r["turn_12m"])
    assert r["status"] == "reduce"                                            # 5.2× < 8× - 10%
    assert r["ideal_pcs"] == pytest.approx(round(r["annual_sales"] / 8))
    assert _one(stock, sales, target_turn=5.2)["status"] == "keep"
    assert _one(stock, sales, target_turn=4)["status"] == "increase"


def test_zero_stock_fortnights_count_in_the_average():
    pcs = {d: (10 if i % 2 == 0 else 0) for i, d in enumerate(DATES)}         # in stock half the time
    r = _one(_stock(pcs), _sales([(AS_OF, 5)]))
    assert r["avg_pcs_12m"] == pytest.approx(5)                               # not 10, as Stock_ETL had it
    assert r["avail_12m"] == 13 and not r["q_12m"]                            # 50% < 60%


def test_new_product_blends_3_and_6_months_75_25():
    pcs = {d: (6 if i < 12 else 0) for i, d in enumerate(DATES)}              # launched about 5.5 months ago
    sales = _sales([(AS_OF - pd.Timedelta(days=10 * k), 1) for k in range(16)])
    r = _one(_stock(pcs), sales)
    assert r["pattern"] == "3m+6m" and r["weights"] == "75/25/0"
    assert r["blended_turn"] == pytest.approx(0.75 * r["turn_3m"] + 0.25 * r["turn_6m"])


def test_only_3_months_is_too_new():
    pcs = {d: (4 if i < 5 else 0) for i, d in enumerate(DATES)}
    r = _one(_stock(pcs), _sales([(AS_OF, 2)]))
    assert r["pattern"] == "3m" and r["status"] == "new" and r["change_pcs"] == 0


def test_dead_stock_is_kept_with_turn_zero():
    r = _one(_stock({d: 3 for d in DATES}), _sales([]).reindex(columns=_sales([(AS_OF, 1)]).columns))
    assert r["turn_12m"] == 0 and r["status"] == "reduce" and r["ideal_pcs"] == 0


def test_missing_snapshots_do_not_count_against_availability():
    pcs = {d: 5 for i, d in enumerate(DATES) if i % 3 != 1}                   # a third of the fortnights have no file
    r = _one(_stock(pcs), _sales([(AS_OF - pd.Timedelta(days=20 * k), 1) for k in range(18)]))
    assert r["data_12m"] < 26 and r["avail_12m"] == r["data_12m"] and r["q_12m"]


def test_thin_brick_is_held_when_its_neighbours_are_healthy_else_phased_out():
    healthy = {d: 10 for d in DATES}
    sells = [(AS_OF - pd.Timedelta(days=7 * k), 1) for k in range(52)]       # 52 a year on 10 pcs = 5.2×
    thin = {d: (3 if i % 3 == 0 else 0) for i, d in enumerate(DATES)}        # in stock a third of the time, unsold
    stock = pd.concat([_stock(healthy, "n1", 1), _stock(thin, "b", 2), _stock(healthy, "n2", 3)])
    sales = pd.concat([_sales(sells, "n1", 1), _sales(sells, "n2", 3)])
    t = bt.compute(stock, sales, AS_OF, {**RULES, "target_turn": 5.2}).set_index("brick")
    assert t.loc["n1", "status"] == t.loc["n2", "status"] == "keep"
    assert t.loc["b", "status"] == "hold" and t.loc["b", "change_pcs"] == 0
    t = bt.compute(stock, sales, AS_OF, {**RULES, "target_turn": 20}).set_index("brick")
    assert t.loc["b", "status"] == "phase_out"


def test_rarely_stocked_but_selling_is_stock_up():
    pcs = {d: (2 if i % 4 == 0 else 0) for i, d in enumerate(DATES)}
    r = _one(_stock(pcs), _sales([(AS_OF - pd.Timedelta(days=14 * k), 1) for k in range(26)]))
    assert r["status"] == "stock_up" and r["change_pcs"] > 0


def test_lost_sales_brick_with_no_stock_still_appears():
    stock = _stock({d: 5 for d in DATES}, "other")
    t = bt.compute(stock, _sales([(AS_OF, 2)], "b"), AS_OF, RULES).set_index("brick")
    assert "b" in t.index and t.loc["b", "current_pcs"] == 0 and np.isinf(t.loc["b", "turn_12m"])
