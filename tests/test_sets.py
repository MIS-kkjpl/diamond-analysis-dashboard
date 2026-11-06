import pandas as pd

from kkj.metrics import sets as st


def _stock(rows):
    cols = ["snapshot_date", "barcode", "set_item_barcode", "item_name", "category", "sub_category", "purity",
            "dia_wt", "gross_wt", "pcs", "value", "branch", "location", "age_days"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


D = pd.Timestamp("2026-11-01")
STOCK = _stock([
    (D, "DPD1", "DPS7", "D PENDANT", "FANCY", "", 75, 0.4, 3.0, 1, 60_000, "KJ1", "F", 100),
    (D, "DTP1", "DPS7", "D TOPS", "STUD", "", 75, 0.3, 2.0, 1, 40_000, "KJ1", "F", 100),
    (D, "DRN1", "SDRN3", "D RING", "SOL", "", 75, 0.1, 4.0, 1, 50_000, "KJ1", "F", 30),
    (D, "LOD1", "SDRN3", "LOOSE DIAMOND", "", "", 0, 0.0, 0.7, 1, 150_000, "KJ1", "F", 30),
    (D, "DTP2", "DTP0", "D TOPS", "STUD", "", 75, 0.2, 2.0, 1, 30_000, "KJ1", "F", 10),
    (D, "DKS1", "DKS16", "D KUNDAN SET", "", "", 75, 3.0, 40.0, 3, 300_000, "KJ1", "F", 10),
])


def test_set_id_only_known_multi_label_prefixes():
    assert st.set_id("DPS1644") == "DPS1644"
    assert st.set_id(" sdrn12 ") == "SDRN12"
    assert st.set_id("DTP0") is None           # placeholder
    assert st.set_id("DTP375") is None         # single-piece id
    assert st.set_id("DKS16") is None          # single-label kundan set
    assert st.set_id(None) is None


def test_stock_units_collapse_sets_and_keep_value():
    u = st.stock_units(STOCK)
    assert len(u) == 4                          # pendant set, solitaire ring, single tops, kundan set
    assert u["pcs"].sum() == 4
    assert u["value"].sum() == STOCK["value"].sum()
    dps = u[u["item_name"] == "Diamond Pendant Set"].iloc[0]
    assert dps["members"] == 2 and abs(dps["dia_wt"] - 0.7) < 1e-9
    assert dps["barcode"] == "DPD1"             # the most valuable member leads
    sol = u[u["item_name"] == "Solitaire Diamond Ring"].iloc[0]
    assert abs(sol["dia_wt"] - 0.8) < 1e-9      # ring diamonds + the stone's carats (its gross weight)
    assert sol["barcode"] == "DRN1"             # jewellery leads, not the more valuable stone
    assert sol["purity"] == 75
    kundan = u[u["item_name"] == "D KUNDAN SET"].iloc[0]
    assert kundan["pcs"] == 1 and kundan["component_pcs"] == 3


def test_sales_units_one_per_set_per_bill_and_returns():
    sales = pd.DataFrame({
        "bill_no": ["B1", "B1", "B2", "R1", "R1"],
        "set_item_barcode": ["DPS7", "DPS7", "DTP0", "DPS9", "DPS9"],
        "item_name": ["D PENDANT", "D TOPS", "D TOPS", "D PENDANT", "D TOPS"],
        "category": "", "sub_category": "", "purity": 75,
        "dia_wt": [0.4, 0.3, 0.2, 0.4, 0.3], "gross_wt": 2.0,
        "pcs": [1, 1, 2, -1, -1], "value": [60_000, 40_000, 30_000, -50_000, -30_000]})
    u = st.sales_units(sales)
    assert u["pcs"].tolist().count(1.0) == 2 and u["pcs"].sum() == 1     # set + tops pair, minus a returned set
    assert u["value"].sum() == sales["value"].sum()


def test_broken_sets_sold_separately_and_apart():
    now = STOCK[STOCK["barcode"] != "DTP1"].copy()
    now.loc[now["barcode"] == "LOD1", "location"] = "Vault"
    history = STOCK[["snapshot_date", "barcode", "set_item_barcode", "item_name"]].assign(
        snapshot_date=pd.Timestamp("2026-10-01"))
    sold = pd.DataFrame({"barcode": ["DTP1"], "set_item_barcode": ["DPS7"], "item_name": ["D TOPS"],
                         "sale_date": [pd.Timestamp("2026-10-15")], "bill_no": ["B9"], "sale_branch": ["KJ1"]})
    b = st.broken_sets(now, history, sold).set_index("set_id")
    assert b.loc["DPS7", "problem"] == "part sold separately"
    assert "bill B9" in b.loc["DPS7", "missing"]
    assert b.loc["DPS7", "members_before"] == 2 and b.loc["DPS7", "members_now"] == 1
    assert b.loc["SDRN3", "problem"] == "members in different places"
    assert len(b) == 2


def test_sold_whole_or_split():
    day = pd.Timestamp("2026-10-01")
    sales = pd.DataFrame({"bill_no": ["B1", "B2", "B3", "B4"], "pcs": [1, 1, 1, 1],
                          "sale_date": [day, day, day, day + pd.Timedelta(days=9)],
                          "set_item_barcode": ["DPS1", "DPS1", "DPS2", "DPS2"]})
    t = st.sold_whole_or_split(sales).set_index("set_type")
    assert t.loc["Diamond Pendant Set", "sets_sold"] == 2
    assert t.loc["Diamond Pendant Set", "split"] == 1          # DPS1: two bills, same day = whole


def test_unit_word():
    assert st.unit_word("Diamond Pendant Set") == "sets"
    assert st.unit_word("D KUNDAN SET", 1) == "set"
    assert st.unit_word("Solitaire Diamond Ring") == "sets"
    assert st.unit_word("D TOPS") == "pieces"
