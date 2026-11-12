"""ERP voucher histories (kkj.pipelines.label_vouchers) and how the audit explains movements with them."""
import pandas as pd

from kkj.checks import audit
from kkj.pipelines.label_vouchers import fy_of, series_of


def test_series_and_financial_year():
    assert series_of("MHODDJ-288") == "MHODDJ"
    assert series_of("MK2SOS-1873") == "MK2SOS"
    assert series_of("LK3UB-133") == "LK3UB"
    assert fy_of("2026-04-01") == "2026-27" and fy_of("2026-03-31") == "2025-26" and fy_of("2025-06-15") == "2025-26"


def _v(rows):
    v = pd.DataFrame(rows, columns=["barcode", "fy", "seq", "doc_date", "doc_no", "series", "voucher", "tx_type",
                                    "location", "effect"])
    v["order_no"] = None
    v["doc_date"] = pd.to_datetime(v["doc_date"])
    return v


def test_exits_are_explained_by_the_voucher_that_took_the_label_out():
    left = pd.DataFrame({"barcode": ["A", "B", "C", "D", "E"], "branch": "KJ2", "location": "KJ2 F Counter",
                         "item_class_group": "Diamond Jewellery", "item_amt": 1.0, "why": "UNEXPLAINED",
                         "last_seen": pd.Timestamp("2026-10-01"), "missing_from": pd.Timestamp("2026-10-08")})
    v = _v([["A", "2026-27", 0, "2026-10-03", "MK2booko-1", "MK2booko", "BookedOut", "Out", "KJ2 F Counter", "booking"],
            ["B", "2026-27", 0, "2026-10-05", "MHODDJ-9", "MHODDJ", "Stock Outward", "Out", "HO MD COUNTER", "outward"],
            ["C", "2026-27", 0, "2026-10-06", "MK2SOS-5", "MK2SOS", "Sale", "Out", "KJ2 F Counter", "sale"],
            ["D", "2026-27", 0, "2026-10-06", "MK2XX-1", "MK2XX", "Mystery", "Out", "KJ2 F Counter", "unknown"]])
    fetched = pd.DataFrame({"barcode": ["A", "B", "C", "D"], "fy": "2026-27", "fetched_at": pd.Timestamp("2026-11-12"),
                            "error": None})
    out = audit.explain_left(left, v, fetched, bills={"MK2SB-1"}).set_index("barcode")["erp_why"]
    assert out["A"] == "booked for a customer"
    assert out["B"] == "Stock Outward MHODDJ"
    assert out["C"] == "ERP sale, bill not in our sales"          # a sale our sales data is missing: a real gap
    assert out["D"] == "not classified: Mystery MK2XX"
    assert out["E"] == "history not fetched"


def test_arrivals_prefer_a_released_booking_over_a_transfer():
    arrived = pd.DataFrame({"barcode": ["A"], "branch": "KJ2", "location": "KJ2 F Counter", "item_class_group": "x",
                            "label_date": pd.Timestamp("2025-01-01"), "item_amt": 1.0, "why": "UNEXPLAINED",
                            "not_in": pd.Timestamp("2026-10-01"), "first_seen": pd.Timestamp("2026-10-08")})
    v = _v([["A", "2026-27", 0, "2026-10-03", "MK2ITC-1", "MK2ITC", "Stock Transfer", "In", "KJ2 F Counter", "transfer"],
            ["A", "2026-27", 1, "2026-10-04", "MK2book-1", "MK2book", "BookedIn", "In", "KJ2 F Counter", "booking_return"]])
    fetched = pd.DataFrame({"barcode": ["A"], "fy": "2026-27", "fetched_at": pd.Timestamp("2026-11-12"), "error": None})
    assert audit.explain_arrived(arrived, v, fetched)["erp_why"].iloc[0] == "booking released"
