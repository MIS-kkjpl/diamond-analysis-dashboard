import hashlib, os, re, sys
import pandas as pd
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 40)
BASE = r"Z:\Software Developer\Kartik_R"

def num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")

print("=== SNAPSHOT FILE HASHES ===")
folder = os.path.join(BASE, "Stock_all_in_csv")
hashes = {}
for f in sorted(os.listdir(folder)):
    h = hashlib.md5(open(os.path.join(folder, f), "rb").read()).hexdigest()
    hashes.setdefault(h, []).append(f)
    m = re.search(r"(\d{2})_(\d{2})_(\d{4})", f)
    print(f"{f:28s} {h[:10]}  module2_regex={'OK' if m else 'SKIPPED'}")
print("Duplicates:", [v for v in hashes.values() if len(v) > 1])

print("\n=== SALES CSV ===")
s = pd.read_csv(os.path.join(BASE, "ALL_Data", "SALES2025JUNE_2026JUNEUPDATED.csv"), dtype=str, low_memory=False)
s = s.loc[:, ~s.columns.str.contains("^Unnamed")]
print(s.shape); print(list(s.columns))
s["d"] = pd.to_datetime(s["Date"], format="%d-%b-%y", errors="coerce")
print("bad dates:", s["d"].isna().sum(), "range:", s["d"].min(), s["d"].max())
for c in ["Pcs", "Item Amt", "Gross Wt", "Dia Wt", "Age", "Dia Amt", "Labour Charges"]:
    v = num(s[c]); s[c+"_n"] = v
    print(f"{c}: NaN={v.isna().sum()} neg={(v<0).sum()} zero={(v==0).sum()} sum={v.sum():,.0f}")
print("\nGroups:\n", s["ItemClassification Group"].value_counts().head(25))
dg = ["Diamond Jewellery","Broken Dia Jew","LOOSE DIAMOND","MRP DIAMOND","D Kundan Jewellery"]
d = s[s["ItemClassification Group"].isin(dg)].copy()
print("\nDiamond rows:", len(d), " by group:", d["ItemClassification Group"].value_counts().to_dict())
print("Pcs distribution:", d["Pcs_n"].value_counts().head(8).to_dict())
print("Monthly diamond rows/value:")
print(d.groupby(d["d"].dt.to_period("M")).agg(rows=("Pcs_n","size"), pcs=("Pcs_n","sum"), val=("Item Amt_n","sum"), days=("d", lambda x: x.dt.day.nunique())))
print("\nLocations (diamond):", d["Location"].nunique()); print(d["Location"].value_counts().head(40).to_dict())
print("Locations with '_':", [l for l in d["Location"].dropna().unique() if "_" in l])
print("ItemNames with '_':", [l for l in d["ItemName"].dropna().unique() if "_" in l][:10])
print("Categories:", d["Category"].value_counts().head(20).to_dict())
print("ItemName count:", d["ItemName"].nunique(), d["ItemName"].value_counts().head(25).to_dict())
dup = d.duplicated(["BarCode/BatchDesc"], keep=False)
print("Barcodes sold more than once:", dup.sum())
print(d[dup].sort_values("BarCode/BatchDesc")[["BarCode/BatchDesc","Date","Bill No","Pcs","Item Amt","Location"]].head(12))
print("Bill No prefixes:", d["Bill No"].str.extract(r"^([A-Z]+\d*[A-Z]*)")[0].value_counts().head(20).to_dict())
print("Item Amt NaN examples:", d.loc[d["Item Amt_n"].isna(), "Item Amt"].value_counts().head(5).to_dict())
print("Age at sale (days) describe:\n", d["Age_n"].describe())
d.to_parquet(os.path.join(os.path.dirname(__file__), "sales_diamond.parquet"))
