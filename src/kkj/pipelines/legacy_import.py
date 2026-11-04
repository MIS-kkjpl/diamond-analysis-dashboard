"""
One-off import of the legacy files into the history schemas. No ERP calls.

  sales  ALL_Data/SALES2025JUNE_2026JUNEUPDATED.csv  -> sales.sales_line (source csv_legacy)
  stock  Stock_all_in_csv/<d>_<mm>_<yyyy>_Stock.csv  -> stock_hist.snapshot (source csv_legacy)

Only the diamond item groups are kept (the ones the ERP diamond reports cover).
Run `sales` before `stock`: the stock integrity checks use the imported sales.

Stock snapshot files are checked before loading; a file that fails is recorded in
stock_hist.snapshot_status as 'quarantined' (with its numbers) and not loaded:
  - the checks in kkj.checks.snapshot (labels dated after the snapshot; items
    sold in the 30 days before it still present)
  - duplicate_of:   identical labels and values to another file (a copy)
Legacy files were exported in July 2026, so their Age column is the age on the
export day; age_days is recomputed as snapshot_date - label date (the ERP's own
definition, verified on erp.stock_snapshot) and the file's value kept as
age_days_reported.

Run:
  python -m kkj.pipelines.legacy_import sales [--file PATH] [--check-only]
  python -m kkj.pipelines.legacy_import stock [--folder PATH] [--check-only]
"""
import argparse
import logging
import re
import sys
from pathlib import Path

import pandas as pd

from kkj.checks import snapshot as snapcheck
from kkj.db import history as db
from kkj.erp.stock_report import (DATE_COLUMNS, DECIMAL_COLUMNS, EXCEL_TO_DB, INT_COLUMNS, check,
                                  to_number, to_text)

log = logging.getLogger("kkj")

LEGACY_DIR = Path(r"Z:\Software Developer\Kartik_R")
SALES_CSV = LEGACY_DIR / "ALL_Data" / "SALES2025JUNE_2026JUNEUPDATED.csv"
STOCK_DIR = LEGACY_DIR / "Stock_all_in_csv"
SOURCE = "csv_legacy"

# The item groups the ERP diamond product groups (5, 100, 126, 14, 121, 127, 69, 18) return
DIAMOND_GROUPS = ["Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND",
                  "Broken Dia Jew", "Customer Diamond"]

DATE_FORMATS = ("%d/%m/%Y", "%d-%b-%y")   # the sales CSV switches format in May 2026

SALES_COLUMNS = {
    "Label No": "label_no", "ItemName": "item_name", "Date": "sale_date", "Bill No": "bill_no",
    "Category": "category", "ItemClassification Group": "item_class_group",
    "Location": "location", "BarCode/BatchDesc": "barcode", "Purity": "purity", "Pcs": "pcs",
    "Gross Wt": "gross_wt", "Net Wt": "net_wt", "Dia Pcs": "dia_pcs", "Dia Wt": "dia_wt",
    "Cst Wt": "cst_wt", "Dia Amt": "dia_amt", "Stone Amt": "stone_amt", "Item Amt": "item_amt",
    "Cst Amt": "cst_amt", "Labour Rate": "labour_rate", "Labour Charges": "labour_charges",
    "Age": "age_days_at_sale", "Order No": "order_no",
    "LatestTransactionDate": "latest_txn_date", "Supplier": "supplier", "Fine Wt": "fine_wt",
    "Rate": "rate", "LabourType": "labour_type", "Labeling Date": "labeling_date",
    "Wastage Per": "wastage_per", "Stone Wt": "stone_wt",
}
SALES_INT = ["label_no", "dia_pcs", "age_days_at_sale"]
SALES_DECIMAL = ["purity", "pcs", "gross_wt", "net_wt", "dia_wt", "cst_wt", "dia_amt", "stone_amt",
                 "item_amt", "cst_amt", "labour_rate", "labour_charges", "fine_wt", "rate",
                 "wastage_per", "stone_wt"]
SALES_DATES = ["sale_date", "latest_txn_date", "labeling_date"]

# Legacy stock CSVs have the export's columns except these three
STOCK_COLUMNS = {k: v for k, v in EXCEL_TO_DB.items()
                 if k not in ("Branch", "Taxableamount", "Othercharges")}
STOCK_COLUMNS["Age"] = "age_days_reported"


def to_dates(series):
    """Text dates in any of DATE_FORMATS -> datetime64; anything else -> NaT."""
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in DATE_FORMATS:
        missing = out.isna()
        out[missing] = pd.to_datetime(series[missing], format=fmt, errors="coerce")
    return out


def read_csv(path, columns):
    """Read a legacy CSV as text, keep the expected columns, fail if the layout differs."""
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    raw = raw.loc[:, ~raw.columns.str.startswith("Unnamed")]
    raw.columns = raw.columns.str.strip()
    missing = [c for c in columns if c not in raw.columns]
    extra = [c for c in raw.columns if c not in columns]
    if missing or extra:
        raise ValueError(f"{path.name}: layout changed. Missing {missing}; unexpected {extra}")
    return raw


def convert(raw, columns, ints, decimals, dates):
    """Text frame -> typed frame with DB names; also {column: values that failed to parse}."""
    df, lost = pd.DataFrame(index=raw.index), {}
    for src, col in columns.items():
        cells = raw[src].str.strip().replace("", None)
        if col in ints or col in decimals:
            values = to_number(cells)
            if col in ints:
                values = values.where(values % 1 == 0).astype("Int64")
        elif col in dates:
            values = to_dates(cells).dt.date
        else:
            values = cells.map(to_text)
        lost[col] = int(cells.notna().sum() - pd.Series(values).notna().sum())
        df[col] = values
    return df.reset_index(drop=True), {c: n for c, n in lost.items() if n}


def log_checks(checks, title):
    log.info(f"{title}:")
    for c in checks:
        log.info(f"  {'PASS' if c['passed'] else 'FAIL'}  {c['check']}  ({c['detail']})")


# ── sales ────────────────────────────────────────────────────────────────────
MIN_REBUILD_ACCURACY = 98.0     # % of real barcodes the rebuild rules must reproduce


def barcode_rules(known):
    """{item_name: (prefix, width)} from barcodes of the form PREFIX + label_no.

    width is the zero-padded digit count for items whose barcodes are padded
    (e.g. DKT0002), else 0. Only barcodes whose digits equal the label number count.
    """
    m = known["barcode"].str.extract(r"^([A-Za-z]+)(\d+)$")
    k = known.assign(prefix=m[0], digits=m[1]).dropna(subset=["prefix"])
    k = k[pd.to_numeric(k["digits"]) == pd.to_numeric(k["label_no"])]
    k = k.assign(padded=k["digits"].str.len() > k["label_no"].astype("int64").astype(str).str.len())
    rules = {}
    for name, g in k.groupby("item_name"):
        width = int(g.loc[g["padded"], "digits"].str.len().max()) if g["padded"].any() else 0
        rules[name] = (g["prefix"].mode().iloc[0], width)
    return rules


def rebuild_barcodes(df, rules):
    """Barcode from item_name + label_no (None where the item has no rule or no label no)."""
    def one(name, label):
        if name not in rules or pd.isna(label):
            return None
        prefix, width = rules[name]
        return prefix + str(int(label)).zfill(width)
    return pd.Series([one(n, l) for n, l in zip(df["item_name"], df["label_no"])], index=df.index)


def import_sales(path, check_only):
    log.info(f"Reading {path}")
    raw = read_csv(path, SALES_COLUMNS)
    total_rows = len(raw)
    raw = raw[raw["ItemClassification Group"].isin(DIAMOND_GROUPS)]
    df, lost = convert(raw, SALES_COLUMNS, SALES_INT, SALES_DECIMAL, SALES_DATES)
    branch_map = db.location_branch_map()
    df["branch"] = df["location"].map(branch_map)
    date_from, date_to = df["sale_date"].min(), df["sale_date"].max()
    log.info(f"{total_rows:,} rows in file, {len(df):,} diamond rows, {date_from} to {date_to}")

    # May–Dec 2025: the barcode column holds the bill no. Rebuild it from the item's
    # barcode rule (learned from production stock only), after proving the rule on
    # the rows that do carry a real barcode.
    rules = barcode_rules(db.stock_barcodes())
    rebuilt = rebuild_barcodes(df, rules)
    is_bill = df["barcode"] == df["bill_no"]
    real = ~is_bill & rebuilt.notna()
    accuracy = round(100 * (rebuilt[real] == df.loc[real, "barcode"]).mean(), 2)
    df["barcode_rebuilt"] = is_bill & rebuilt.notna()
    df.loc[df["barcode_rebuilt"], "barcode"] = rebuilt[df["barcode_rebuilt"]]
    still_bill = int((df["barcode"] == df["bill_no"]).sum())
    log.info(f"Barcode rebuild: {int(is_bill.sum()):,} lines had the bill no; rules for "
             f"{len(rules)} item names reproduce {accuracy}% of {int(real.sum()):,} real barcodes; "
             f"{int(df['barcode_rebuilt'].sum()):,} rebuilt, {still_bill} left as the bill no")

    checks = [
        check("Every sale date parsed", df["sale_date"].notna().all(),
              f"{int(df['sale_date'].isna().sum())} unparsed"),
        check("No unparseable numbers/dates", not lost, str(lost) if lost else "0 lost"),
        check("No null barcode", df["barcode"].notna().all(), f"{int(df['barcode'].isna().sum())} null"),
        check(f"Barcode rule reproduces >= {MIN_REBUILD_ACCURACY}% of real barcodes",
              accuracy >= MIN_REBUILD_ACCURACY, f"{accuracy}% of {int(real.sum()):,}"),
    ]
    age_calc = (pd.to_datetime(df["sale_date"]) - pd.to_datetime(df["labeling_date"])).dt.days
    unmapped = df.loc[df["branch"].isna(), "location"].value_counts()
    dq = {
        "barcode_rebuilt_from_label": int(df["barcode_rebuilt"].sum()),
        "barcode_still_bill_no": still_bill,
        "barcode_still_bill_no_items": df.loc[df["barcode"] == df["bill_no"], "item_name"].value_counts().to_dict(),
        "age_equals_sale_minus_labeling_pct": round(100 * (age_calc == df["age_days_at_sale"]).mean(), 1),
        "locations_without_branch": unmapped.to_dict(),
        "by_group": df["item_class_group"].value_counts().to_dict(),
    }
    log_checks(checks, "Validation")
    log.info(f"Data quality: {dq}")
    if check_only or not all(c["passed"] for c in checks):
        return all(c["passed"] for c in checks)

    load_id = db.start_sales_load(SOURCE, date_from, date_to, raw_file=str(path), record_count=len(df))
    try:
        rows = db.replace_sales(df, SOURCE, date_from, date_to, load_id)
        stored = db.sales_totals(SOURCE, date_from, date_to)
        checks += [
            check("DB count == diamond rows in file", stored["count"] == len(df), f"{stored['count']} vs {len(df)}"),
            check("DB sum pcs == file", abs(stored["pcs"] - float(df["pcs"].sum())) <= 0.01,
                  f"{stored['pcs']:,.3f} vs {float(df['pcs'].sum()):,.3f}"),
            check("DB sum item_amt == file", abs(stored["item_amt"] - float(df["item_amt"].sum())) <= 0.01,
                  f"{stored['item_amt']:,.2f} vs {float(df['item_amt'].sum()):,.2f}"),
        ]
        log_checks(checks[-3:], "Database check")
        ok = all(c["passed"] for c in checks)
        db.finish_sales_load(load_id, "success" if ok else "failed", rows, checks, dq_warnings=dq,
                             error=None if ok else "database check failed")
        log.info(f"Load {load_id}: {rows:,} rows, {'success' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        db.finish_sales_load(load_id, "failed", 0, checks, error=f"{type(e).__name__}: {e}"[:2000])
        raise


# ── stock ────────────────────────────────────────────────────────────────────
def snapshot_date_from_name(path):
    m = re.match(r"(\d{1,2})_(\d{1,2})_(\d{4})_Stock$", path.stem)
    if not m:
        raise ValueError(f"Can't read <d>_<mm>_<yyyy>_Stock from {path.name}")
    d, mth, y = map(int, m.groups())
    return pd.Timestamp(year=y, month=mth, day=d).date()


def read_stock_file(path):
    """Typed diamond rows of one snapshot file, plus file-level checks."""
    raw = read_csv(path, STOCK_COLUMNS)
    raw = raw[raw["Sr.No"].str.strip() != ""]          # trailing blank/footer rows
    sr = to_number(raw["Sr.No"])
    file_checks = [check("Rows == max(Sr.No)", len(raw) == sr.max(), f"{len(raw)} vs {sr.max():.0f}")]
    raw = raw[raw["ItemClassification Group"].isin(DIAMOND_GROUPS)]
    df, lost = convert(raw, STOCK_COLUMNS, INT_COLUMNS + ["age_days_reported"],
                       DECIMAL_COLUMNS, DATE_COLUMNS)
    file_checks.append(check("No unparseable numbers/dates", not lost, str(lost) if lost else "0 lost"))
    file_checks.append(check("No null barcode", df["barcode"].notna().all(),
                             f"{int(df['barcode'].isna().sum())} null"))
    file_checks.append(check("Barcodes unique", not df["barcode"].duplicated().any(),
                             f"{int(df['barcode'].duplicated().sum())} duplicated"))
    return df, file_checks


def import_stock(folder, check_only):
    files = sorted(folder.glob("*_Stock.csv"), key=snapshot_date_from_name)
    log.info(f"{len(files)} snapshot files in {folder}")
    branch_map = db.location_branch_map()
    results = []
    for path in files:
        snapshot_date = snapshot_date_from_name(path)
        df, file_checks = read_stock_file(path)
        df["branch"] = df["location"].map(branch_map)
        df["age_days"] = snapcheck.recomputed_age(df, snapshot_date)
        results.append({"path": path, "date": snapshot_date, "df": df, "file_checks": file_checks,
                        "integrity": snapcheck.integrity(df, snapshot_date)})
        log.info(f"  {path.name}: {len(df):,} diamond labels")

    # Duplicate groups by signature
    by_sig = {}
    for r in results:
        by_sig.setdefault(r["integrity"]["signature"], []).append(r["date"])
    for r in results:
        i = r["integrity"]
        i["duplicate_of"] = [str(d) for d in by_sig[i["signature"]] if d != r["date"]]
        r["reasons"] = [c["check"] for c in r["file_checks"] if not c["passed"]] + snapcheck.reasons(i)
    # A copy whose group has more than one member passing the checks: no way to tell which date is real
    for sig, dates in by_sig.items():
        group = [r for r in results if r["integrity"]["signature"] == sig]
        if len(group) > 1 and sum(not r["reasons"] for r in group) > 1:
            for r in group:
                r["reasons"].append(f"identical to {', '.join(r['integrity']['duplicate_of'])}; real date unknown")

    table = pd.DataFrame([{
        "date": r["date"], "labels": r["integrity"]["labels"],
        "lbl_after%": r["integrity"]["labeled_after_pct"],
        "sold_prev30": r["integrity"]["sold_prev_days"], "still_present%": r["integrity"]["sold_before_pct"],
        "export_day": r["integrity"]["implied_export_date"],
        "copy_of": ",".join(r["integrity"]["duplicate_of"]),
        "status": "quarantined" if r["reasons"] else "loaded"} for r in results])
    log.info("\n" + table.to_string(index=False))
    for r in results:
        if r["reasons"]:
            log.info(f"  {r['date']} quarantined: {'; '.join(r['reasons'])}")
    if check_only:
        return True

    ok = True
    for r in results:
        status = {"file": r["path"].name, "status": "quarantined" if r["reasons"] else "loaded",
                  "reason": "; ".join(r["reasons"]) or None, "labels": r["integrity"]["labels"],
                  "checks": {"file": r["file_checks"], **{k: v for k, v in r["integrity"].items()}}}
        rows = db.replace_stock_hist(None if r["reasons"] else r["df"], r["date"], SOURCE, status)
        if rows:
            stored = db.stock_hist_totals(r["date"], SOURCE)
            same = (stored["count"] == len(r["df"])
                    and abs(stored["item_amt"] - float(r["df"]["item_amt"].sum())) <= 0.01)
            ok &= same
            log.info(f"  {r['date']}: loaded {rows:,}; read back {stored['count']:,} rows, "
                     f"item_amt {stored['item_amt']:,.2f} {'OK' if same else 'MISMATCH'}")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import legacy sales/stock files into the history schemas.")
    sub = ap.add_subparsers(dest="what", required=True)
    s = sub.add_parser("sales"); s.add_argument("--file", type=Path, default=SALES_CSV)
    t = sub.add_parser("stock"); t.add_argument("--folder", type=Path, default=STOCK_DIR)
    for p in (s, t):
        p.add_argument("--check-only", action="store_true", help="read and check; write nothing")
    args = ap.parse_args(argv)
    log.addHandler(logging.StreamHandler(sys.stdout)); log.setLevel(logging.INFO)
    ok = (import_sales(args.file, args.check_only) if args.what == "sales"
          else import_stock(args.folder, args.check_only))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
