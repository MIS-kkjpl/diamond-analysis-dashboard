"""
Five years of sales, every product group, from the ERP's Criteria-wise Sales Search exports
(one .xlsx per financial year, 2021-22 onwards) -> sales.hist_line. For seasonality.

Each file is read by header name (the column order differs between years), and loads only if
its row count and its sums of pieces, gross weight and item value equal the file's own totals
row exactly. Re-running a file replaces its rows. No ERP calls.

Run:  python -m kkj.pipelines.history_import [--check-only] [files ...]
      (default: the five CriteriaWise Sales Search files in the legacy data folder)
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from sqlalchemy import text

from kkj import settings

DATA_DIR = Path(r"Z:\Software Developer\Kartik_R\ALL_Data")
# 2025-26: the "_amt" export of the same report (Apr 2025 - Jan 2026); the plain one lacks the value columns
FILES = [f"CriteriaWise Sales Search {fy}.xlsx" for fy in ("21-22", "22-23", "23-24", "24-25")] + ["25 - 26_amt.xlsx"]
COLUMNS = {   # header in the export -> column in sales.hist_line
    "Sr.No": "sr_no", "Date": "sale_date", "Bill No": "bill_no", "Label No": "label_no",
    "BarCode/BatchDesc": "barcode", "ItemName": "item_name", "Category": "category", "Sub Category": "sub_category",
    "ItemClassification Group": "item_class_group", "Location": "location", "Purity": "purity", "Pcs": "pcs",
    "Gross Wt": "gross_wt", "Net Wt": "net_wt", "Dia Wt": "dia_wt", "Item Amt": "item_amt", "Supplier": "supplier",
    "Age": "age_days", "Order No": "order_no",
}
SUMS = {"pcs": 3, "gross_wt": 3, "item_amt": 2}     # checked against the totals row, exactly at this precision


def read_export(path):
    """(rows DataFrame, totals {column: value}, row count stated in the totals row)."""
    wb = load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    ws.reset_dimensions()
    header, rows, totals, stated = None, [], {}, None
    for r in ws.iter_rows(values_only=True):
        first = r[0] if r else None
        if header is None:
            if first == "Sr.No":
                header = [str(c).strip() if c is not None else "" for c in r]
            continue
        if isinstance(first, (int, float)):
            rows.append(r[:len(header)])
            continue
        label = next((c for c in r if isinstance(c, str) and c.strip().startswith("Total (")), None)
        if label:                       # the totals row: "Total (N)" in some column, the sums under their columns
            stated = int(label.strip()[len("Total ("):].rstrip(")"))
            totals = {h: v for h, v in zip(header, r) if h and isinstance(v, (int, float))}
    wb.close()
    if header is None:
        raise ValueError(f"{path.name}: no header row (Sr.No) found")
    df = pd.DataFrame(rows, columns=header)
    missing = [h for h in COLUMNS if h not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: columns missing {missing}")
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    return df, {COLUMNS[h]: v for h, v in totals.items() if h in COLUMNS}, stated


def clean(df):
    out = df.copy()
    out["sale_date"] = pd.to_datetime(out["sale_date"].astype(str).str.strip(), format="%d/%m/%Y", errors="coerce")
    for c in ("sr_no",):
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    for c in ("purity", "pcs", "gross_wt", "net_wt", "dia_wt", "item_amt", "age_days"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in ("bill_no", "label_no", "barcode", "item_name", "category", "sub_category", "item_class_group",
              "location", "supplier", "order_no"):
        out[c] = out[c].map(lambda v: None if v is None or str(v).strip() in ("", "nan", "None") else
                            (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()))
    return out


def checks(df, totals, stated, name):
    rows = [("Rows == Total (N)", stated == len(df), f"{len(df)} vs {stated}"),
            ("Every date parsed", df["sale_date"].notna().all(), f"{int(df['sale_date'].isna().sum())} unparsed"),
            ("Serial numbers unique", not df["sr_no"].duplicated().any(), f"{int(df['sr_no'].duplicated().sum())} repeated")]
    for col, dec in SUMS.items():
        if col in totals and totals[col] not in (None, ""):
            got, want = round(float(df[col].sum()), dec), round(float(totals[col]), dec)
            rows.append((f"Sum {col} == totals row", got == want, f"{got:,.{dec}f} vs {want:,.{dec}f}"))
    for check, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {check}  ({detail})")
    return all(ok for _, ok, _ in rows)


def load(df, name):
    with settings.etl_engine().begin() as conn:
        conn.execute(text("DELETE FROM sales.hist_line WHERE source_file = :f"), {"f": name})
        df.assign(source_file=name).to_sql("hist_line", conn, schema="sales", if_exists="append", index=False,
                                           method="multi", chunksize=2000)
        back = conn.execute(text("SELECT count(*), sum(pcs), sum(item_amt) FROM sales.hist_line WHERE source_file = :f"),
                            {"f": name}).one()
    ok = back[0] == len(df) and round(float(back[2] or 0), 2) == round(float(df["item_amt"].sum()), 2)
    print(f"  Loaded {back[0]:,} rows; read back {'OK' if ok else 'MISMATCH'}")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Five years of sales exports -> sales.hist_line (seasonality)")
    ap.add_argument("files", nargs="*", help="export files (default: the five yearly files)")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args(argv)
    paths = [Path(f) for f in a.files] or [DATA_DIR / f for f in FILES]
    results = []
    for path in paths:
        t0 = datetime.now()
        print(f"{path.name}:")
        raw, totals, stated = read_export(path)
        df = clean(raw)
        print(f"  {len(df):,} rows, {df['sale_date'].min():%d %b %Y} to {df['sale_date'].max():%d %b %Y}, "
              f"read in {(datetime.now() - t0).seconds} s")
        ok = checks(df, totals, stated, path.name)
        if ok and not a.check_only:
            ok = load(df, path.name)
        elif not ok:
            print("  NOT loaded: a check failed")
        results.append(ok)
    print(f"{sum(results)}/{len(results)} files OK")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
