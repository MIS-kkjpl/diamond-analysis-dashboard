"""
Business Studio's 7 stock categories from several ERP pulls (config/stock_categories.json).

Business Studio takes the category from which export a row came from, so each pull carries its category.
The main diamond pull is split per set (set code, or the piece's own barcode): a set with any loose diamond
(or an SD set code) is Loose; one with any D Kundan Jewellery / Kundan / Polki piece (or a DPK set code) is
Kundan; watches are Watch; the rest is Diamond. Customer Diamond is not loaded.
Value is the ERP Item Amt plus Other Charges, as in Business Studio's files.
"""
import json

import pandas as pd

from kkj import settings

CATEGORIES_FILE = settings.CONFIG_DIR / "stock_categories.json"
CATEGORIES = ["diamond", "kundan", "loose", "platinum", "stone", "stone_mala", "watch"]


def load_config(path=CATEGORIES_FILE):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    used = [p["category"] for p in cfg["pulls"]] + [r["category"] for r in cfg["main_rules"]["set_rules"]]
    unknown = sorted(set(used) - set(CATEGORIES))
    if unknown:
        raise ValueError(f"Unknown categories in {path.name}: {unknown}")
    return cfg


def set_key(df):
    """A set's code, or the piece's own barcode when it is not in a set."""
    return df["set_item_barcode"].where(df["set_item_barcode"].notna() & df["set_item_barcode"].ne(""),
                                        df["barcode"]).astype(str).str.strip()


def main_categories(df, rules):
    """Category per row of the main pull, decided per set."""
    key = set_key(df)
    name = df["item_name"].fillna("").str.upper()
    group = df["item_class_group"].fillna("")
    cat = pd.Series("diamond", index=df.index)
    decided = pd.Series(False, index=df.index)
    for r in rules["set_rules"]:
        hit = group.isin(r.get("any_group", []))
        for word in r.get("name_contains", []):
            hit |= name.str.contains(word, regex=False)
        hit |= key.str.startswith(tuple(r.get("set_prefix", []))) if r.get("set_prefix") else False
        sets = set(key[hit])
        now = ~decided & key.isin(sets)
        cat[now] = r["category"]
        decided |= now
    return cat


def assign(frames, cfg):
    """frames: {pull name: cleaned df}. Returns (rows to load with stock_category, pull, stock_value; counts left out)."""
    rules = cfg["main_rules"]
    parts, left_out = [], {}
    for p in cfg["pulls"]:
        df = frames[p["name"]].assign(stock_category=p["category"], pull=p["name"])
        if p["name"] == "main":
            not_loaded = df["item_class_group"].isin(rules["not_loaded_groups"])
            left_out = {f"group {g}": int(df["item_class_group"].eq(g).sum()) for g in rules["not_loaded_groups"]}
            df = df[~not_loaded].copy()
            df["stock_category"] = main_categories(df, rules)
        parts.append(df)
    out = pd.concat(parts, ignore_index=True)
    out["stock_value"] = out["item_amt"].fillna(0) + out["other_charges"].fillna(0)
    return unique_barcodes(out), left_out


def unique_barcodes(df):
    """Batch stock (loose stones) keeps one barcode across locations: such rows get code@location, then
    code@location#sr_no if still shared, so (snapshot_date, barcode) stays unique."""
    df = df.copy()
    for suffix in (lambda d: "@" + d["location"].fillna(""), lambda d: "#" + d["sr_no"].astype(str)):
        shared = df["barcode"].duplicated(keep=False)
        if not shared.any():
            break
        df.loc[shared, "barcode"] = df.loc[shared, "barcode"] + suffix(df.loc[shared])
    return df
