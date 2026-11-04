"""Snapshot integrity: for each snapshot, (a) overlap with neighbours,
(b) of diamond labels sold in (prev, this], how many still appear in this snapshot (should be ~0),
(c) implied 'export day' from Age vs LabelingDate."""
import os, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "archive" / "Stock_ETL"))
import config
HERE = os.path.dirname(os.path.abspath(__file__))
e = config.get_engine()
st = pd.read_sql("select snapshot_date, barcode_batchdesc bc from diamond_stock_master", e)
snaps = {d: set(g.bc) for d, g in st.groupby("snapshot_date")}

# raw tables carry age + labeling date -> implied export date = labeling_date + age
implied = {}
for d in snaps:
    t = f"stock_{d:%Y_%m_%d}"
    r = pd.read_sql(f'select "age", "labeling_date" from {t} where "itemclassification_group" = \'Diamond Jewellery\' limit 400', e)
    age = pd.to_numeric(r["age"].str.replace(",", ""), errors="coerce")
    ld = pd.to_datetime(r["labeling_date"], format="%d/%m/%Y", errors="coerce")
    implied[d] = (ld + pd.to_timedelta(age, unit="D")).mode().iloc[0]

s = pd.read_parquet(os.path.join(HERE, "sales_diamond.parquet"))
raw = pd.read_csv(r"Z:\Software Developer\Kartik_R\ALL_Data\SALES2025JUNE_2026JUNEUPDATED.csv", dtype=str, usecols=["Date","BarCode/BatchDesc","ItemClassification Group"])
dg = ["Diamond Jewellery","Broken Dia Jew","LOOSE DIAMOND","MRP DIAMOND","D Kundan Jewellery"]
raw = raw[raw["ItemClassification Group"].isin(dg)]
raw["d"] = pd.to_datetime(raw["Date"], format="%d-%b-%y", errors="coerce").fillna(pd.to_datetime(raw["Date"], format="%d/%m/%Y", errors="coerce"))

ds = sorted(snaps)
rows = []
for i, d in enumerate(ds):
    prev = ds[i-1] if i else None
    sold = set(raw[(raw.d > prev) & (raw.d <= d)]["BarCode/BatchDesc"]) if prev is not None else set()
    still = len(sold & snaps[d])
    rows.append({"snapshot": d.date(), "labels": len(snaps[d]),
                 "overlap_prev": len(snaps[d] & snaps[prev]) if prev is not None else None,
                 "new_vs_prev": len(snaps[d] - snaps[prev]) if prev is not None else None,
                 "sold_since_prev": len(sold), "sold_but_still_present": still,
                 "implied_export_date": implied[d].date() if pd.notna(implied[d]) else None})
print(pd.DataFrame(rows).to_string())
