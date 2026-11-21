"""
Business Studio's stock rules, ported line for line so Stockwise shows the same numbers.

Sources (archive/inventory_project/references/kkjpl-design-studio-master): utils/analytics/stockBuckets.ts
(mapBranch, wtRng, stoneWtRng, amtRng, ageRng, normalizeName), utils/stockLocationClassifier.ts (getOtherGroup),
utils/kkjClassification.ts (getGender, getPattern), utils/analytics/parseStock.ts (how they are used).

Business Studio counts one row per set (its export clubs a set into one row), so the bands are cut on each
set's totals: units() rolls pieces up to sets first. A set's age is its newest piece (when the set was made).
"""
import re

import numpy as np
import pandas as pd

from kkj.erp.stock_categories import load_config, set_key

BRANCH_CODES = ["KJ1", "KJ2", "KJ3", "KJ4", "KJ5", "KJ7", "KJ8"]
HEAD_OFFICE = "Head Office"
KNOWN_PREFIXES = {"HO": HEAD_OFFICE, "HEAD": HEAD_OFFICE, "MO": "Mumbai Office"}

WT_BANDS = ["0-1 ct", "1-2 ct", "2-4 ct", "4-8 ct", "8-12 ct", "12-20 ct", "20+ ct"]
WT_EDGES = [1, 2, 4, 8, 12, 20]
STONE_WT_EDGES = [2, 5, 10, 20, 50, 100]
STONE_WT_BANDS = [f"{0 if i == 0 else STONE_WT_EDGES[i - 1]}-{e} ct" for i, e in enumerate(STONE_WT_EDGES)] + ["100+ ct"]
AMT_BANDS = ["0-50K", "50K-1L", "1L-2.5L", "2.5L-5L", "5L-10L", "10L-25L", "25L+"]
AMT_EDGES = [50_000, 100_000, 250_000, 500_000, 1_000_000, 2_500_000]
AGE_BANDS = ["0-90d", "91-180d", "181-365d", "1-2yr", "2+yr"]
AGE_UPPER = [90, 180, 365, 730]          # inclusive upper bounds
NOT_TAGGED = "Not tagged"                # Business Studio's visible bucket for rows without a band value

PATTERN_MAP = {
    "MICRO": "Micro", "SUPER MICRO": "Super Micro", "GENERIC": "Generic", "SUPER GENERIC": "Super Generic",
    "BRIDAL POLKI SET": "Bridal Polki", "H.SET": "H Set", "H SET": "H Set", "H .SET": "H Set",
    "DESIGNER": "Designer", "DESIGNER SET": "Designer", "DESIGNER P SET": "Designer",
    "CLASSIC": "Classic", "CLASSIC STUDD": "Classic", "CLASSIC DESIGNER": "Classic",
    "COLOR STONE N.SET": "Colour Stone", "COLOUR STONE": "Colour Stone", "COLOUR STONE BRACELET": "Colour Stone",
    "D KUNDAN BANGLE": "Kundan", "POLKI": "Polki", "OPEN POLKI": "Polki", "D. POLKI KADA": "Polki",
    "BRIDLE JADAU SET": "Kundan", "JADAU KADA": "Kundan", "LOOSE DIAMOND": "Loose", "WATCH MRP": "Watch",
    "F D WATCH FIX": "Watch", "D H SET": "H Set", "D PENDANT SET": "Generic", "D POLKI KUNDAN SET": "Bridal Polki",
    "D P SET": "Generic",
}


def other_group(loc):
    """getOtherGroup: a location that is not a KJ branch, grouped by its first word."""
    first = loc.split(" ")[0].upper()
    if first in KNOWN_PREFIXES:
        return KNOWN_PREFIXES[first]
    if first in BRANCH_CODES:
        return "In Transit" if "TRANSIT" in loc.upper() else "Approval"
    return first


def map_branch(loc):
    """mapBranch: KJ code found anywhere in the location (spaces ignored), Exhibition, else other_group."""
    if loc is None or (isinstance(loc, float) and np.isnan(loc)) or str(loc) == "":
        return "Others"
    squashed = re.sub(r"\s+", "", str(loc).upper())
    for b in BRANCH_CODES:
        if b in squashed:
            return b
    trimmed = str(loc).strip()
    if trimmed.upper() == "EXHIBITION COUNTER":
        return "Exhibition"
    return other_group(trimmed)


def is_showroom(branch):
    """isKnownStockBranch: the KJ branches (and Exhibition / Others) that Business Studio charts as branches."""
    return branch in ("Exhibition", "Others") or any(str(branch).upper().startswith(c) for c in BRANCH_CODES)


def normalize_name(name):
    """normalizeName: drop a leading 'BROKEN ' and a trailing ' FIX', squash spaces."""
    s = re.sub(r"^BROKEN\s+", "", str(name or ""), flags=re.I)
    s = re.sub(r"\s+FIX$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _band(values, edges, labels, right=False):
    """Bands for positive values; 0 / blank -> Not tagged (Business Studio's bands return null for them)."""
    v = pd.to_numeric(values, errors="coerce")
    idx = np.searchsorted(edges, v.fillna(0).to_numpy(), side="left" if right else "right")
    out = pd.Series(np.array(labels, dtype=object)[np.minimum(idx, len(labels) - 1)], index=values.index)
    return out.where(v.fillna(0) > 0, NOT_TAGGED)


def wt_band(dia_wt):
    return _band(dia_wt, WT_EDGES, WT_BANDS)


def stone_wt_band(net_wt):
    return _band(net_wt, STONE_WT_EDGES, STONE_WT_BANDS)


def amt_band(value):
    return _band(value, AMT_EDGES, AMT_BANDS)


def age_band(age_days):
    return _band(age_days, AGE_UPPER, AGE_BANDS, right=True)


def _gender_of(text):
    upper = str(text or "").upper().strip()
    if not upper:
        return None
    if upper == "G WATCH":
        return "Gents"
    if upper == "L WATCH":
        return "Ladies"
    if re.search(r"\bGENTS\b", upper):
        return "Gents"
    if re.search(r"\bLADIES\b", upper) or re.search(r"\bLEDIES\b", upper):
        return "Ladies"
    return None


def gender(category, item_name):
    """getGender(Category) || getGender(ItemName); neither -> Not tagged."""
    return pd.Series([_gender_of(c) or _gender_of(n) or NOT_TAGGED for c, n in zip(category, item_name)],
                     index=category.index)


def pattern(category):
    """getPattern: the Category string looked up in Business Studio's pattern list, else Other."""
    return category.fillna("").str.upper().str.strip().map(PATTERN_MAP).fillna("Other")


def units(stock):
    """One row per set (Business Studio's clubbed row) with the set's totals and the bands cut on them.

    stock: rows of erp.stock_snapshot_bs. Batch stones are already one row per barcode@location.
    """
    names = {k: v for k, v in load_config().get("location_names", {}).items() if not k.startswith("_")}
    # loose diamonds hold their carats in Net Wt; Business Studio's Dia Wt adds them in
    loose_ct = stock["net_wt"].where(stock["item_class_group"].eq("LOOSE DIAMOND"), 0).fillna(0)
    df = stock.assign(unit=set_key(stock), location=stock["location"].replace(names),
                      dia_wt=stock["dia_wt"].fillna(0) + loose_ct)
    # Business Studio clubs a set within each file, so a set code spread over two categories is two rows
    first = df.sort_values("sr_no").groupby(["stock_category", "unit"], sort=False)
    u = first.agg(location=("location", "first"),
                  item_name=("item_name", "first"), category=("category", "first"),
                  item_class_group=("item_class_group", "first"), supplier=("supplier", "first"),
                  age_days=("age_days", "min"), labels=("barcode", "size"), pcs=("pcs", "sum"),
                  value=("stock_value", "sum"), dia_wt=("dia_wt", "sum"), net_wt=("net_wt", "sum"),
                  cst_wt=("cst_wt", "sum"), stone_wt=("stone_wt", "sum")).reset_index()
    u["branch"] = u["location"].map(map_branch)
    u["name"] = u["item_name"].map(normalize_name)
    stoney = u["stock_category"].isin(["stone", "stone_mala"])
    u["wt_band"] = wt_band(u["dia_wt"]).where(~stoney, stone_wt_band(u["net_wt"]))
    u["amt_band"] = amt_band(u["value"])
    u["age_band"] = age_band(u["age_days"])
    u["gender"] = gender(u["category"], u["item_name"])
    u["pattern"] = pattern(u["category"])
    return u
