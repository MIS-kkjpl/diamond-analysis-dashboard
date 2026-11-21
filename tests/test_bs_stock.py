"""Business Studio stock rules (kkj.metrics.bs_stock) against the cases its own code defines."""
import pandas as pd

from kkj.metrics import bs_stock as b


def test_branch_follows_map_branch():
    cases = {"KJ2 F Counter": "KJ2", "KJ 3 A Counter": "KJ3", "Exhibition Counter": "Exhibition",
             "HO MD COUNTER": "Head Office", "Head Office": "Head Office", "MO Diamond": "Mumbai Office",
             "Approval": "APPROVAL", "Supply Counter": "SUPPLY", "WB in Transit": "WB", "Mumbai Branch": "MUMBAI",
             "PERNIAS": "PERNIAS", None: "Others", "": "Others"}
    assert {k: b.map_branch(k) for k in cases} == cases
    assert b.is_showroom("KJ2") and b.is_showroom("Exhibition") and not b.is_showroom("Head Office")


def test_bands_use_business_studio_edges():
    s = pd.Series
    assert b.wt_band(s([0, 0.5, 1, 3.99, 4, 19.9, 20, None])).tolist() == [
        "Not tagged", "0-1 ct", "1-2 ct", "2-4 ct", "4-8 ct", "12-20 ct", "20+ ct", "Not tagged"]
    assert b.amt_band(s([49_999, 50_000, 2_500_000])).tolist() == ["0-50K", "50K-1L", "25L+"]
    assert b.age_band(s([0, 1, 90, 91, 365, 366, 730, 731])).tolist() == [
        "Not tagged", "0-90d", "0-90d", "91-180d", "181-365d", "1-2yr", "1-2yr", "2+yr"]
    assert b.stone_wt_band(s([1.9, 2, 100, 0])).tolist() == ["0-2 ct", "2-5 ct", "100+ ct", "Not tagged"]


def test_names_gender_pattern():
    assert b.normalize_name("BROKEN D  RING FIX") == "D RING"
    g = b.gender(pd.Series(["G WATCH", "LADIES RING", None, "MICRO"]), pd.Series(["x", "y", "GENTS KADA", "D RING"]))
    assert g.tolist() == ["Gents", "Ladies", "Gents", "Not tagged"]
    assert b.pattern(pd.Series(["super micro", "H.SET", None, "RING"])).tolist() == ["Super Micro", "H Set", "Other", "Other"]


def test_units_roll_a_set_into_one_row():
    stock = pd.DataFrame({
        "sr_no": [1, 2, 3], "barcode": ["A1", "A2", "B1"], "set_item_barcode": ["DPS1", "DPS1", None],
        "stock_category": ["diamond"] * 3, "location": ["KJ2 F Counter"] * 3, "item_name": ["D PENDANT", "D TOPS", "D RING"],
        "category": ["MICRO", "MICRO", None], "item_class_group": ["Diamond Jewellery"] * 3, "supplier": ["S"] * 3,
        "age_days": [10, 12, 400], "pcs": [1, 2, 1], "stock_value": [60_000, 50_000, 30_000],
        "dia_wt": [0.6, 0.5, 2.0], "net_wt": [3, 2, 4], "cst_wt": [0, 0, 0], "stone_wt": [0, 0, 0]})
    u = b.units(stock).set_index("unit")
    assert len(u) == 2
    assert u.loc["DPS1", "value"] == 110_000 and u.loc["DPS1", "amt_band"] == "1L-2.5L"
    assert u.loc["DPS1", "wt_band"] == "1-2 ct" and u.loc["DPS1", "labels"] == 2
    assert u.loc["B1", "age_band"] == "1-2yr" and u.loc["B1", "branch"] == "KJ2"
