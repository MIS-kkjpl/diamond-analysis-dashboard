"""Label positions and days on display from voucher histories (kkj.metrics.display)."""
import pandas as pd

from kkj.metrics import display as dp


def _v(rows):
    v = pd.DataFrame(rows, columns=["barcode", "fy", "seq", "doc_date", "tx_type", "location", "order_no", "effect"])
    v["cancelled"] = False
    return v


def test_a_label_back_at_its_counter_the_same_day_is_still_there():
    v = _v([["A", "2026-27", 0, "2026-03-31", "In", "KJ2 F Counter", None, "transfer"],
            ["A", "2026-27", 3, "2026-06-20", "1", "KJ2 F Counter", None, "no_effect"],      # tag reprint: no move
            ["A", "2026-27", 1, "2026-11-10", "In", "KJ2 Approval", None, "transfer"],
            ["A", "2026-27", 2, "2026-11-10", "In", "KJ2 F Counter", None, "transfer"]])     # back the same day
    pos = dp.positions(v)
    assert len(pos) == 1 and pd.isna(pos["end"].iloc[0])                                    # one open stay
    assert dp.where_on(pos, "2026-11-11")["A"] == "KJ2 F Counter"


def test_a_sale_ends_the_stay_and_a_booking_holds_until_released():
    v = _v([["S", "2026-27", 0, "2026-03-31", "In", "KJ2 F Counter", None, "transfer"],
            ["S", "2026-27", 1, "2026-05-01", "Out", "KJ2 F Counter", None, "sale"],
            ["B", "2026-27", 0, "2026-03-31", "In", "KJ5 F Counter", None, "transfer"],
            ["B", "2026-27", 1, "2026-05-30", "Out", "KJ5 F Counter", "MK5C-105", "booking"],
            ["B", "2026-27", 2, "2026-05-30", "In", "KJ5 F Counter", "MK5C-105", "booking"],
            ["B", "2026-27", 3, "2026-06-21", "In", "KJ2 F Counter", None, "transfer"],         # still booked
            ["B", "2026-27", 4, "2026-07-01", "Out", "KJ2 F Counter", None, "booking_return"],
            ["B", "2026-27", 5, "2026-07-01", "In", "KJ2 F Counter", None, "inward"]])          # released (UB)
    pos = dp.positions(v)
    assert "S" not in dp.where_on(pos, "2026-05-02").index
    assert "B" not in dp.where_on(pos, "2026-06-25").index                     # reserved for the customer
    assert dp.where_on(pos, "2026-06-25", reserved=True)["B"] == "KJ2 F Counter"
    assert dp.where_on(pos, "2026-07-02")["B"] == "KJ2 F Counter"               # ordinary stock again
    days = dp.display_days(pos, {"KJ2 F Counter", "KJ5 F Counter"}, until="2026-07-31")
    b = days[days["barcode"] == "B"].groupby("month")["days"].sum()
    assert b[pd.Period("2026-04", "M")] == 30 and b[pd.Period("2026-07", "M")] == 31
    assert pd.Period("2026-06", "M") not in b.index                            # booked all June: not on display
