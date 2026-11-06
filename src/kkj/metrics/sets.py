"""
Sets: pieces sold together, and the selling unit.

The ERP holds a set in one of two ways:
  - one label for the whole set, with pieces = its components (a kundan set of necklace + 2 earrings is
    1 label of 3 pcs; a bangle pair is 1 label of 2 pcs);
  - separate labels sharing a set id in set_item_barcode ('DPS1644' = Diamond Pendant Set no. 1644: a pendant
    label and a tops label). Only the prefixes in SET_TYPES group labels; other ids ('DTP375', 'LOD12') and
    placeholders ('DTP0') belong to single pieces.
Solitaire sets (SD..., SOJ) pair jewellery with its solitaire stone, which is labelled LOOSE DIAMOND and
carries its carats in gross weight.

A SELLING UNIT is a set (either form) or a single piece. Counting units keeps a pendant set from counting
as a pendant plus a pair of tops, and a kundan set from counting as three pieces. A multi-label set
becomes one row named after its set type, with the members' carats and value summed.

  set_id(value)          'DPS1644' -> 'DPS1644'; 'DTP375', 'DTP0', None -> None
  stock_units(df)        stock label rows -> one row per selling unit
  sales_units(df)        sale lines -> one row per unit sold (a set on one bill = 1; a return = -1)
  broken_sets(now, hist) sets in stock now missing members they had before
  sold_whole_or_split(s) per set type: sold whole on one bill vs split
"""
import re

import numpy as np
import pandas as pd

# multi-label set prefixes -> set type. DPS, DHS and SDRN were named by the business; the others are read from
# what their members are (e.g. SDTP = tops + solitaire stone) and can be renamed here.
SET_TYPES = {
    "DPS": "Diamond Pendant Set", "DHS": "Diamond Half Set", "SDRN": "Solitaire Diamond Ring",
    "DPK": "Diamond Polki/Kundan Set", "SDTP": "Solitaire Diamond Tops", "SDPD": "Solitaire Diamond Pendant",
    "SDPT": "Solitaire Diamond Pendant Set", "SDNK": "Solitaire Diamond Necklace",
    "SDBC": "Solitaire Diamond Bracelet", "SDMS": "Solitaire Diamond Mangalsutra",
    "SOJ": "Solitaire Jewellery Set",
}
SET_ID = re.compile(r"^([A-Z]+)[1-9]\d*$")
LOOSE = ("LOOSE DIAMOND", "LOOSE DIAMOND (C)")
SET_WORDS = ("SET", "SOLITAIRE")


def set_id(value):
    """The multi-label set a label belongs to, or None."""
    v = str(value or "").strip().upper()
    m = SET_ID.match(v)
    return v if m and m.group(1) in SET_TYPES else None


def set_type(sid):
    return SET_TYPES.get(SET_ID.match(sid).group(1), sid) if sid else None


def is_set_product(item_name):
    """True for products sold as sets (multi-label set types and single-label sets like D KUNDAN SET)."""
    name = str(item_name or "").upper()
    return any(w in name for w in SET_WORDS)


def unit_word(item_name, n=2):
    """What a unit of this product is called on a card: 'set(s)' or 'piece(s)'."""
    if is_set_product(item_name):
        return "set" if n == 1 else "sets"
    return "piece" if n == 1 else "pieces"


def _carats(df):
    """Diamond carats per label: the diamond weight, or for a loose stone (no diamond weight) its gross weight."""
    dia = pd.to_numeric(df["dia_wt"], errors="coerce").fillna(0)
    gross = pd.to_numeric(df["gross_wt"], errors="coerce").fillna(0)
    return dia.where((dia != 0) | ~df["item_name"].isin(LOOSE), gross)


def _collapse(rows, keys):
    """One row per (keys): the lead member's columns (most valuable piece of jewellery, not the solitaire stone),
    named after the set type, with the members' carats, gross weight and value summed."""
    r = rows.assign(_carats=_carats(rows), _loose=rows["item_name"].isin(LOOSE),
                    _v=pd.to_numeric(rows["value"], errors="coerce").fillna(0))
    lead = r.sort_values(["_loose", "_v"], ascending=[True, False]).drop_duplicates(keys)
    sums = r.groupby(keys, dropna=False).agg(dia_wt=("_carats", "sum"), gross_wt=("gross_wt", "sum"),
                                             value=("value", lambda v: v.sum(min_count=1)),
                                             members=("item_name", "size"),
                                             member_names=("item_name", lambda v: " + ".join(sorted(set(v)))))
    out = lead.drop(columns=["dia_wt", "gross_wt", "value", "_carats", "_loose", "_v"]).merge(
        sums.reset_index(), on=keys, how="left")
    out["item_name"] = out["set_id"].map(set_type)
    out["category"] = ""
    out["sub_category"] = ""
    return out


def stock_units(stock, keys=("snapshot_date",)):
    """Stock label rows -> one row per selling unit (per snapshot); pcs is 1 per unit, component_pcs keeps the label's."""
    s = stock.assign(set_id=stock["set_item_barcode"].map(set_id))
    in_set = s["set_id"].notna()
    single = s[~in_set].assign(members=1)
    grouped = _collapse(s[in_set], [k for k in keys if k in s] + ["set_id"]) if in_set.any() else None
    out = pd.concat([single, grouped], ignore_index=True) if grouped is not None else single
    out["component_pcs"] = out["pcs"]
    out["pcs"] = 1.0
    return out


def sales_units(sales):
    """Sale lines -> one row per unit sold: a set's labels sold the same day count once (on one bill or several);
    a return counts -1. Without sale_date, the bill decides."""
    pcs = pd.to_numeric(sales["pcs"], errors="coerce").fillna(0)
    s = sales.assign(set_id=sales["set_item_barcode"].map(set_id), _sign=np.sign(pcs))
    in_set = s["set_id"].notna()
    single = s[~in_set].assign(members=1)
    when = "sale_date" if "sale_date" in s else "bill_no"
    grouped = _collapse(s[in_set], [when, "set_id", "_sign"]) if in_set.any() else None
    out = pd.concat([single, grouped], ignore_index=True) if grouped is not None else single
    out["component_pcs"] = out["pcs"]
    out["pcs"] = out["_sign"].astype(float)
    return out[out["pcs"] != 0].drop(columns="_sign")


BROKEN_COLS = ["set_id", "set_type", "problem", "left_in_stock", "where", "branch", "missing", "value_left", "age_days",
               "members_now", "members_before"]
PLACE_SEP = " | "            # between places in 'where' (branch names themselves contain ' / ')


def broken_sets(now, history, sold=None):
    """Sets in stock now that cannot be sold whole: a member has left stock (sold separately, or moved out), or
    the members sit in different places.

    now:     latest stock labels (barcode, set_item_barcode, item_name, branch, location, value, age_days)
    history: every label stocked under those set ids (snapshot_date, barcode, set_item_barcode, item_name)
    sold:    sales of those labels (barcode, sale_date, bill_no, sale_branch), to say how a missing member left
    """
    n = now.assign(set_id=now["set_item_barcode"].map(set_id)).dropna(subset=["set_id"])
    if n.empty:
        return pd.DataFrame(columns=BROKEN_COLS)
    sold = sold if sold is not None else pd.DataFrame(columns=["barcode", "sale_date", "bill_no", "sale_branch"])
    h = history.assign(set_id=history["set_item_barcode"].map(set_id)).dropna(subset=["set_id"])
    h = h[h["set_id"].isin(set(n["set_id"]))]
    gone = h[~h.set_index(["set_id", "barcode"]).index.isin(n.set_index(["set_id", "barcode"]).index)]
    gone = gone.sort_values("snapshot_date").drop_duplicates(["set_id", "barcode"], keep="last")
    # a set id can hold two of the same kind (two tops-and-stone pairs): a member is only missing when nothing of
    # its kind is left in the set
    kinds_left = set(zip(n["set_id"], n["item_name"]))
    gone = gone[[k not in kinds_left for k in zip(gone["set_id"], gone["item_name"])]]
    gone = gone.merge(sold[["barcode", "sale_date", "bill_no", "sale_branch"]], on="barcode", how="left")

    def how(r):
        last = pd.Timestamp(r.snapshot_date).strftime("%d %b %Y")
        if pd.notna(r.sale_date):
            return f"{r.item_name} {r.barcode}: sold {pd.Timestamp(r.sale_date):%d %b %Y}, bill {r.bill_no}"
        return f"{r.item_name} {r.barcode}: left stock after {last}, no sale found"
    gone_t = gone.assign(_how=[how(r) for r in gone.itertuples()]).groupby("set_id").agg(
        missing=("_how", "; ".join), gone_n=("barcode", "nunique"), any_sold=("sale_date", lambda v: v.notna().any()))

    n = n.assign(_desc=n["item_name"] + " " + n["barcode"], _place=n["branch"].fillna("") + " · " + n["location"].fillna(""))
    out = n.groupby("set_id").agg(
        left_in_stock=("_desc", ", ".join), where=("_place", lambda v: PLACE_SEP.join(sorted(set(v)))),
        places=("_place", "nunique"), branch=("branch", lambda v: v.iloc[0] if v.nunique() == 1 else None), value_left=("value", "sum"), age_days=("age_days", "max"),
        members_now=("barcode", "nunique")).join(gone_t)
    out = out[out["gone_n"].notna() | (out["places"] > 1)].copy()
    if out.empty:
        return pd.DataFrame(columns=BROKEN_COLS)
    out["gone_n"] = out["gone_n"].fillna(0).astype(int)
    out["members_before"] = out["members_now"] + out["gone_n"]
    out["missing"] = out["missing"].fillna("")
    out["problem"] = np.select(
        [(out["gone_n"] > 0) & out["any_sold"].fillna(False).astype(bool), out["gone_n"] > 0],
        ["part sold separately", "part left stock"], "members in different places")
    out["set_type"] = out.index.map(set_type)
    return out.reset_index().sort_values("value_left", ascending=False)[BROKEN_COLS]


def sold_whole_or_split(sales):
    """Per set type: sets sold whole vs split (its pieces sold on different days).

    sales: sale lines with sale_date and set_item_barcode. Pieces sold the same day count as whole even on separate
    bills (one customer is often billed twice). A set with a piece still in stock is not seen here (see broken_sets).
    """
    pcs = pd.to_numeric(sales["pcs"], errors="coerce")
    s = sales[pcs > 0].assign(set_id=lambda d: d["set_item_barcode"].map(set_id)).dropna(subset=["set_id"])
    cols = ["set_type", "sets_sold", "sold_whole", "split", "whole_share"]
    if s.empty:
        return pd.DataFrame(columns=cols)
    per = s.groupby("set_id").agg(days=("sale_date", "nunique"))
    per["set_type"] = per.index.map(set_type)
    t = per.groupby("set_type").agg(sets_sold=("days", "size"), split=("days", lambda d: int((d > 1).sum())))
    t["sold_whole"] = t["sets_sold"] - t["split"]
    t["whole_share"] = t["sold_whole"] / t["sets_sold"]
    return t.reset_index().sort_values("sets_sold", ascending=False)[cols]
