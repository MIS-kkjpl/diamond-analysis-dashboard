import pandas as pd

from kkj.metrics import sellers as sl

RULES = {"price_band_edges": [50_000, 100_000]}
D = pd.Timestamp("2026-10-01")


def _line(bill, who, pcs, label, billed, branch="KJ2", orig=None, age=100):
    return {"sale_date": D, "bill_no": bill, "salesman": who, "pcs": pcs, "item_amt": label, "taxable_amount": billed,
            "net_value": billed, "net_pcs": pcs, "branch": branch, "orig_bill_no": orig, "age_days_at_sale": age,
            "set_item_barcode": "DTP0", "item_class_group": "Diamond Jewellery", "item_name": "D TOPS"}


PEOPLE = pd.DataFrame({"sales_code": ["K0001", "K0002", "K0009"], "name": ["Asha", "Ravi", "Owner"],
                       "branch": ["KJ2", "KJ2", "KJ2"], "kind": ["salesperson", "salesperson", "owner"],
                       "active": [True, True, True]})


def test_returns_go_to_the_original_seller_and_owners_are_left_out():
    lines = pd.DataFrame([_line("B1", "1 Asha", 1, 60_000, 57_000),
                          _line("R1", "2 Ravi", -1, 60_000, -57_000, orig="B1"),
                          _line("B2", "9 Owner", 1, 60_000, 60_000)])
    a = sl.attribute(lines, PEOPLE)
    assert set(a["code"]) == {"K0001"}                     # the return is Asha's; the owner's sale is dropped
    card = sl.scorecard(a, RULES, 365).set_index("code")
    assert card.loc["K0001", "net_sales"] == 0 and card.loc["K0001", "return_rate"] == 1


def test_discount_is_compared_with_colleagues_in_the_same_price_band():
    lines = pd.DataFrame([_line("B1", "1 Asha", 1, 60_000, 54_000),       # 10% off
                          _line("B2", "2 Ravi", 1, 60_000, 58_800),       # 2% off
                          _line("B3", "2 Ravi", 1, 20_000, 20_000)])      # other band: not Asha's peer
    card = sl.scorecard(sl.attribute(lines, PEOPLE), RULES, 365).set_index("code")
    assert abs(card.loc["K0001", "discount"] - 0.10) < 1e-9
    assert abs(card.loc["K0001", "peer_discount"] - 0.06) < 1e-9          # mean of 10% and 2% in that band
    assert card.loc["K0001", "discount_vs_peers"] > 0 > card.loc["K0002", "discount_vs_peers"]
