import os
import pandas as pd
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 50)
BASE = r"Z:\Software Developer\Kartik_R"
HERE = os.path.dirname(os.path.abspath(__file__))

def num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")

s = pd.read_csv(os.path.join(BASE, "ALL_Data", "SALES2025JUNE_2026JUNEUPDATED.csv"), dtype=str, low_memory=False)
bad = s[pd.to_datetime(s["Date"], format="%d-%b-%y", errors="coerce").isna()]
print("Bad date samples:", bad["Date"].value_counts().head(15).to_dict())
alt = pd.to_datetime(bad["Date"], dayfirst=True, errors="coerce")
print("Re-parsed dayfirst range:", alt.min(), alt.max(), "still bad:", alt.isna().sum())
dg = ["Diamond Jewellery","Broken Dia Jew","LOOSE DIAMOND","MRP DIAMOND","D Kundan Jewellery"]
print("Bad-date diamond rows:", bad["ItemClassification Group"].isin(dg).sum())
print(bad[bad["ItemClassification Group"].isin(dg)].groupby(alt.dt.to_period("M")).size())

# Sales value sanity: item_amt vs components
d = pd.read_parquet(os.path.join(HERE, "sales_diamond.parquet"))
print("\nBill no suffix types:", d["Bill No"].str.extract(r"^[LM]K?\d?([A-Z]+)-")[0].value_counts().to_dict())
print("L vs M prefix:", d["Bill No"].str[0].value_counts().to_dict())
print("Label No dupes (diamond):", d.duplicated("Label No", keep=False).sum())

print("\n=== ONE OLD SNAPSHOT (30_06_2026) ===")
st = pd.read_csv(os.path.join(BASE, "Stock_all_in_csv", "30_06_2026_Stock.csv"), dtype=str, low_memory=False)
st = st.loc[:, ~st.columns.str.contains("^Unnamed")]
print(st.shape)
print("Groups:", st["ItemClassification Group"].value_counts().head(25).to_dict())
sd = st[st["ItemClassification Group"].isin(dg)].copy()
for c in ["Pcs", "Item Amt", "Age", "Dia Wt"]:
    sd[c] = num(sd[c])
print("Diamond rows:", len(sd), "pcs", sd["Pcs"].sum(), "value", f"{sd['Item Amt'].sum():,.0f}", "Age NaN", sd["Age"].isna().sum())
print("Locations:", sd["Location"].value_counts().to_dict())
print("Barcode dupes:", sd.duplicated("BarCode/BatchDesc").sum())

print("\n=== LATEST ERP PARQUET ===")
p = pd.read_parquet(os.path.join(os.path.dirname(HERE), "..", "data", "stock", "clean", "stock_latest.parquet"))
print(p.shape, p["snapshot_date"].iloc[0])
print("Groups:", p["item_class_group"].value_counts().to_dict())
print("Branches:", p["branch"].value_counts().to_dict())
print("Locations:", p["location"].value_counts().head(40).to_dict())
pd_ = p[p["item_class_group"].isin(dg)]
print("Diamond-group value:", f"{pd_['item_amt'].sum():,.0f}", "labels", len(pd_), "pcs", pd_["pcs"].sum())
print("Age describe:\n", pd_["age_days"].describe())
print("age_days vs days_from_branch_in corr:", pd_[["age_days","days_from_branch_in"]].corr().iloc[0,1])
print("Value by age bucket %:")
b = pd.cut(pd_["age_days"], [-1,30,90,180,365,730,1e9], labels=["0-30","31-90","91-180","181-365","1-2y","2y+"])
print((pd_.groupby(b, observed=False)["item_amt"].sum()/pd_["item_amt"].sum()).round(3).to_dict())
print("Approval locations value:", f"{pd_[pd_['location'].str.contains('pproval', na=False)]['item_amt'].sum():,.0f}")
print("supplier nunique", pd_["supplier"].nunique(), " item_name nunique", pd_["item_name"].nunique(), " category nunique", pd_["category"].nunique())
# what is item_amt for stock? compare to sum of components
comp = pd_[["dia_amt","stone_amt","cst_amt","labour_charges","wastage_amount","other_charges","value_by_todays_rate","taxable_amount"]].sum()
print("Component sums:", {k: f"{v:,.0f}" for k, v in comp.items()})
print(pd_[["item_name","category","net_wt","rate","dia_amt","stone_amt","labour_charges","wastage_amount","item_amt","value_by_todays_rate","taxable_amount"]].head(8))

# Do historical snapshot CSVs and the ERP parquet share the same grain/location names?
print("\nSnapshot-CSV locations not in ERP parquet:", sorted(set(sd["Location"]) - set(pd_["location"])))
print("Sales locations not in ERP parquet:", sorted(set(d["Location"]) - set(pd_["location"])))
