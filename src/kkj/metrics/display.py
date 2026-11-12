"""
Where each label was, day by day, from its ERP voucher history (erp.label_voucher), and from that the days a
product was on display at each branch (kkj.metrics.display).

Each voucher row says where the label is after it: "In" at a location = it is there from that day (a transfer row
names the destination; the year's opening entry, dated 31 March, names where it stood); "Out" = it left stock
(sale, Stock Outward, ...). Rows of other types (tag reprints) change nothing. Each financial-year company holds its
own rows; a label's years are read in order, so its last position in one year runs on until the next year's opening.

A label counts as ON DISPLAY on the days it stands at a location whose kind is 'counter' (config.location) and is
not reserved for a customer: approval, transit, repair, safes and dispatch points are stock, but no customer can see
the piece there. Reserved = from a booking voucher or any voucher carrying a customer order number until a release
(effect booking_return, e.g. the UB "unbooked" pair); the stock report leaves such labels out too.
"""
import numpy as np
import pandas as pd

MOVES = ("In", "Out")


COLUMNS = ["barcode", "location", "reserved", "start", "end"]


def positions(v):
    """One row per (barcode, stay): location, reserved, start (first day there), end (first day no longer there;
    NaT = still there at the end of the history). v: erp.label_voucher rows (barcode, fy, seq, doc_date, tx_type,
    location, cancelled, and optionally order_no and effect from config.voucher_type)."""
    v = v[~v["cancelled"].fillna(False).astype(bool)].copy()
    if v.empty:
        return pd.DataFrame(columns=COLUMNS)
    v["doc_date"] = pd.to_datetime(v["doc_date"])
    v = v.sort_values(["barcode", "fy", "doc_date", "seq"])
    # reserved for a customer: the movement itself carries an order number or is a booking (BookedOut / BookedIn);
    # a later movement without one (a release such as UB, or a plain transfer) means it is ordinary stock again
    effect = v["effect"] if "effect" in v else pd.Series(None, index=v.index, dtype=object)
    if "order_no" in v:
        o = v["order_no"].fillna("").astype(str).str.strip()
        order = (o != "") & (o != "0")
    else:
        order = pd.Series(False, index=v.index)
    # a booking (BookedOut / BookedIn) holds until a release (booking_return, e.g. UB), even when later transfer rows
    # leave the order number out; an order number on any other movement holds for that movement only
    booked = pd.Series(np.where(effect == "booking", 1.0, np.where(effect == "booking_return", 0.0, np.nan)),
                       index=v.index).groupby(v["barcode"]).ffill().fillna(0).astype(bool)
    v["reserved"] = (booked | order) & (effect != "booking_return")
    v = v[v["tx_type"].isin(MOVES)]
    if v.empty:
        return pd.DataFrame(columns=COLUMNS)
    # the state at the end of each day: the last row of that day per label
    day = v.groupby(["barcode", "doc_date"], sort=False).tail(1)
    day = day.assign(where=np.where(day["tx_type"] == "In", day["location"], None))
    day = day.sort_values(["barcode", "doc_date"])
    nxt = day.groupby("barcode")["doc_date"].shift(-1)
    out = pd.DataFrame({"barcode": day["barcode"].to_numpy(), "location": day["where"].to_numpy(),
                        "reserved": day["reserved"].to_numpy(), "start": day["doc_date"].to_numpy(),
                        "end": nxt.to_numpy()})
    out = out[out["location"].notna()]
    # merge consecutive stays at the same place (e.g. a year's closing position and the next year's opening)
    out = out.sort_values(["barcode", "start"])
    same = ((out["barcode"] == out["barcode"].shift()) & (out["location"] == out["location"].shift())
            & (out["reserved"] == out["reserved"].shift()) & (out["start"] == out["end"].shift()))
    out["stay"] = (~same).cumsum()
    first = out.groupby("stay").head(1).set_index("stay")
    last_end = out.groupby("stay").tail(1).set_index("stay")["end"]       # NaT (still there) must survive
    return first.assign(end=last_end)[COLUMNS].reset_index(drop=True)


def where_on(pos, day, reserved=False):
    """barcode -> location on `day` (end of day); labels reserved for a customer left out unless reserved=True."""
    day = pd.Timestamp(day)
    hit = pos[(pos["start"] <= day) & (pos["end"].isna() | (pos["end"] > day))]
    if not reserved:
        hit = hit[~hit["reserved"]]
    return hit.drop_duplicates("barcode", keep="last").set_index("barcode")["location"]


def display_days(pos, counters, until, months=None):
    """Days on display per (barcode, location, month): stays at a counter location, cut into calendar months.
    counters: set of counter locations; until: the day the history runs to (open stays end there)."""
    p = pos[pos["location"].isin(counters) & ~pos["reserved"]].copy()
    until = pd.Timestamp(until) + pd.Timedelta(days=1)
    p["end"] = p["end"].fillna(until).clip(upper=until)
    p = p[p["end"] > p["start"]]
    rows = []
    for b, loc, s, e in zip(p["barcode"], p["location"], p["start"], p["end"]):
        m = s.to_period("M")
        while m.start_time < e:
            lo, hi = max(s, m.start_time), min(e, m.end_time.normalize() + pd.Timedelta(days=1))
            if hi > lo:
                rows.append((b, loc, m, (hi - lo).days))
            m += 1
    out = pd.DataFrame(rows, columns=["barcode", "location", "month", "days"])
    if months is not None:
        out = out[out["month"].isin(months)]
    return out
