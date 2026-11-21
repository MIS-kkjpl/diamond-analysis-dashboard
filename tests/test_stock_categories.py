"""Business Studio category rules (kkj.erp.stock_categories), offline."""
import pandas as pd

from kkj.erp.stock_categories import assign, load_config

COLS = ["sr_no", "barcode", "set_item_barcode", "item_name", "item_class_group", "location", "item_amt",
        "other_charges"]


def frame(rows):
    return pd.DataFrame(rows, columns=COLS)


def test_assign_follows_business_studio():
    cfg = load_config()
    frames = {
        "main": frame([
            (1, "A1", None, "D RING", "Diamond Jewellery", "KJ1", 10, None),
            (2, "S1a", "SDRN1", "D RING", "Diamond Jewellery", "KJ1", 20, None),     # set with a loose diamond
            (3, "S1b", "SDRN1", "LOOSE DIAMOND", "LOOSE DIAMOND", "KJ1", 30, None),  # -> whole set loose
            (4, "L1", None, "LOOSE DIAMOND (C)", "LOOSE DIAMOND", "KJ3", 5, None),   # loose stone itself
            (5, "P1", None, "D POLKI SET", "D Kundan Jewellery", "KJ2", 40, None),   # polki -> kundan
            (6, "K1", None, "F D KUNDAN SET", "MRP DIAMOND", "KJ2", 50, 7),          # + other charges
            (7, "M1", "DPK9", "MANGALSUTRA D", "Diamond Jewellery", "KJ2", 8, None),  # DPK set code -> kundan
            (8, "W1", None, "F D WATCH FIX", "MRP DIAMOND", "KJ2", 60, 3),
            (9, "C1", None, "CUSTOMER DIAMOND", "Customer Diamond", "KJ3", 0, None),  # not loaded
        ]),
        "platinum": frame([(1, "P7", None, "PT RING", "PLATINUM", "KJ1", 70, None)]),
        "stone": frame([(1, "S1/1", None, "RUBY (MQ)", "LOOSE STONE", "Approval", 5, None),   # batch: 2 places
                        (2, "S1/1", None, "RUBY (MQ)", "LOOSE STONE", "MO Diamond", 6, None)]),
        "stonemala": frame([(1, "M9", None, "STONE MALA FIX", "MRP STONE", "KJ3", 9, None)]),
    }
    out, left_out = assign(frames, cfg)
    cat = dict(zip(out["barcode"], out["stock_category"]))
    assert cat == {"A1": "diamond", "S1a": "loose", "S1b": "loose", "L1": "loose", "P1": "kundan", "K1": "kundan",
                   "M1": "kundan", "W1": "watch", "P7": "platinum", "S1/1@Approval": "stone",
                   "S1/1@MO Diamond": "stone", "M9": "stone_mala"}
    assert left_out == {"group Customer Diamond": 1}
    assert out["barcode"].is_unique
    value = dict(zip(out["barcode"], out["stock_value"]))
    assert value["K1"] == 57 and value["W1"] == 63 and value["A1"] == 10
