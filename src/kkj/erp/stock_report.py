"""
Item-Wise Current Stock report: fetch from the ERP, clean the Excel export, validate.

The ERP builds this report in two steps, like the browser does:
  Step A  getLabelwiseCurrentStkRpt  runs the report on the server (~20 s) and
          returns a reportID plus serverTotal.recordCount.
  Step B  getItemWiseCurrentStkRpt   exports that reportID as an Excel file.
Step B only works for a reportID the server issued in step A; a made-up
reportID gives "Records Not Found".

No database code here (see kkj.db); the pipeline is kkj.pipelines.stock.
Ported unchanged from archive/inventory_project/Phase_one.py.
"""
import copy
import json
import logging
import re
import time
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

from kkj import settings
from kkj.erp.client import (JSON_HEADERS, ErpError, Unauthorized, login, new_session, post,
                            server_messages)

# openpyxl warns on every ERP export; the file reads fine without a default style
warnings.filterwarnings("ignore", message="Workbook contains no default style",
                        category=UserWarning)

log = logging.getLogger("kkj.stock")

# ── Which report to fetch ────────────────────────────────────────────────────
REPORT_NAME = "itemwise_current_stock"                              # used in output file names
RUN_ENDPOINT = "acme-inventory-web/inv/getLabelwiseCurrentStkRpt"   # step A: run report
EXPORT_ENDPOINT = "acme-inventory-web/inv/getItemWiseCurrentStkRpt"  # step B: export it
LOCATION_CODES = []                                  # [] = all branches
PRODUCT_CODES = [5, 100, 126, 14, 121, 127, 69, 18]  # the Diamond product groups
ITEM_CODES = []                                      # [] = all items

# Fields that belong to step B only; step A must not receive them
STEP_B_ONLY = ("reportID", "sortedBy", "exportReport")

# ── Excel export -> erp.stock_snapshot ───────────────────────────────────────
HEADER_FIRST_CELL = "Sr.No"          # header row = first row whose column 0 is this
DATE_FORMAT = "%d/%m/%Y"

# Every Excel header and its DB column. A header missing from this dict (or a
# dict entry missing from the file) stops the run: the export layout changed.
EXCEL_TO_DB = {
    "Sr.No": "sr_no",
    "Label No": "label_no",
    "Category": "category",
    "ItemClassification Group": "item_class_group",
    "Branch": "branch",
    "Location": "location",
    "Doc No": "doc_no",
    "BarCode/BatchDesc": "barcode",
    "ItemName": "item_name",
    "Setitembarcode": "set_item_barcode",
    "Purity": "purity",
    "Pcs": "pcs",
    "Gross Wt": "gross_wt",
    "Net Wt": "net_wt",
    "Dia Pcs": "dia_pcs",
    "Dia Wt": "dia_wt",
    "Cst Wt": "cst_wt",
    "Dia Amt": "dia_amt",
    "Stone Amt": "stone_amt",
    "Item Amt": "item_amt",
    "Cst Amt": "cst_amt",
    "Labour Rate": "labour_rate",
    "Labour Charges": "labour_charges",
    "Age": "age_days",
    "Order No": "order_no",
    "Lot No": "lot_no",
    "LatestTransactionDate": "latest_txn_date",
    "Supplier": "supplier",
    "Valuebytodaysrate": "value_by_todays_rate",
    "Fine Wt": "fine_wt",
    "Rate": "rate",
    "LabourType": "labour_type",
    "Labeling Date": "labeling_date",
    "Wastage Per": "wastage_per",
    "Stone Wt": "stone_wt",
    "Sub Category": "sub_category",
    "Label Dt": "label_dt",
    "Wastageamount": "wastage_amount",
    "Scmitem": "scm_item",
    "BranchInDate": "branch_in_date",
    "DaysFromBranchIn": "days_from_branch_in",
    "Taxableamount": "taxable_amount",
    "Othercharges": "other_charges",
}
# Column types (DB names, matching sql/schema.sql); every other column is text
INT_COLUMNS = ["sr_no", "label_no", "dia_pcs", "age_days", "days_from_branch_in"]
DECIMAL_COLUMNS = ["purity", "pcs", "gross_wt", "net_wt", "dia_wt", "cst_wt", "dia_amt",
                   "stone_amt", "item_amt", "cst_amt", "labour_rate", "labour_charges",
                   "value_by_todays_rate", "fine_wt", "rate", "wastage_per", "stone_wt",
                   "wastage_amount", "taxable_amount", "other_charges"]
DATE_COLUMNS = ["latest_txn_date", "labeling_date", "label_dt", "branch_in_date"]
SUM_COLUMNS = ["pcs", "gross_wt", "net_wt", "dia_pcs", "dia_wt", "item_amt"]
SUM_TOLERANCE = 0.01

# ── Data-quality warnings (logged and stored, never block the load) ──────────
CUSTOMER_GROUP = "Customer Diamond"  # item_class_group of customers' own items
# Purity values the karat column in sql/schema.sql understands:
# 0 = No Metal, 37..100 = 9K..24K. Anything else is a warning.
KNOWN_PURITY_MIN, KNOWN_PURITY_MAX = 37, 100


# ── Request bodies ───────────────────────────────────────────────────────────
def build_filters(cfg, log_code=None):
    """stock_payload.json's params with our filters applied and step-B-only fields removed."""
    if not settings.STOCK_PAYLOAD_FILE.exists():
        raise ErpError(f"Missing file: {settings.STOCK_PAYLOAD_FILE}")
    p = json.loads(settings.STOCK_PAYLOAD_FILE.read_text(encoding="utf-8"))["params"]

    # allItemCodes / filterStr in the payload were captured for one set of
    # product groups. They are kept as-is for now; warn if the groups differ.
    if sorted(cfg["products"]) != sorted(p.get("productCodeList", [])):
        log.warning("  Warning: product codes differ from stock_payload.json; allItemCodes and "
                    "filterStr still belong to the original product groups.")

    p["locationCodeList"] = cfg["locations"]
    p["productCodeList"] = cfg["products"]
    p["itemCodeList"] = cfg["items"]
    p["labelDate"] = cfg["date"]
    if cfg.get("as_on"):
        # Stock as it stood on labelDate (the screen's "as on date" option); without
        # these flags a past labelDate still returns today's stock
        p.update(isLabelAsOnDate=1, calStkAsOnLabelDate=True, flagForItem=False)
    if log_code:
        p["logCode"] = log_code
    for key in STEP_B_ONLY:
        p.pop(key, None)
    return p


def step_a_body(filters):
    return {"params": filters}


def step_b_body(filters, report_id):
    params = copy.deepcopy(filters)
    params.update({"reportID": report_id, "sortedBy": [], "exportReport": 1})
    return {"params": params, "printFormat": "excel", "filterOn": {"expLst": []}}


def save_json(data, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(f"  Saved: {rel(path)}")


def rel(path):
    """Path relative to the project folder, for readable logs."""
    try:
        return Path(path).resolve().relative_to(settings.PROJECT_DIR)
    except ValueError:
        return Path(path)


# ── Step A: run the report on the server ─────────────────────────────────────
def run_step_a(session, cfg, filters, meta_path):
    log.info(f"Step A: running {cfg['name']} (as on {cfg['date']})... usually ~20 s")
    t0 = time.time()
    r = post(session, cfg["run_endpoint"], "step A (run report)",
             json=step_a_body(filters), headers=JSON_HEADERS)
    log.info(f"  HTTP {r.status_code} in {time.time() - t0:.1f} s, {len(r.content) / 1024:.0f} kB")
    if r.status_code in (401, 403):
        raise Unauthorized(f"Step A: HTTP {r.status_code}")
    if not r.ok:
        raise ErpError(f"Step A failed: HTTP {r.status_code}.")

    try:
        data = r.json()
    except ValueError:
        raise ErpError("Step A: response is not JSON.")
    # The response does not say which date was asked for; record it for cleaning
    if isinstance(data, dict):
        data["_labelDate"] = cfg["date"]
    save_json(data, meta_path)

    inner = data.get("data") if isinstance(data, dict) else None
    report_id = inner.get("reportID") if isinstance(inner, dict) else None
    if not report_id:
        raise ErpError(f"Step A returned no reportID. Server says: {server_messages(data)}")

    record_count = (inner.get("serverTotal") or {}).get("recordCount")
    log.info(f"  Server generated the report: recordCount = {record_count}")
    if record_count == 0:
        raise ErpError(f"Step A found 0 records. Server says: {server_messages(data)}")
    return report_id, record_count


# ── Step B: export the generated report ──────────────────────────────────────
def run_step_b(session, cfg, filters, report_id, raw_path):
    log.info("Step B: exporting the report...")
    t0 = time.time()
    r = post(session, cfg["export_endpoint"], "step B (export)",
             json=step_b_body(filters, report_id), headers=JSON_HEADERS)
    ctype = r.headers.get("Content-Type", "").lower()
    log.info(f"  HTTP {r.status_code} in {time.time() - t0:.1f} s, "
             f"{len(r.content) / 1024:.0f} kB, {ctype or 'no content-type'}")
    if r.status_code in (401, 403):
        raise Unauthorized(f"Step B: HTTP {r.status_code}")
    if not r.ok:
        raise ErpError(f"Step B failed: HTTP {r.status_code}.")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # .xlsx files are zip archives starting "PK"
    if r.content[:2] == b"PK" or "spreadsheet" in ctype or "excel" in ctype:
        raw_path.write_bytes(r.content)
        log.info(f"  Saved: {rel(raw_path)}")
        return
    # Anything else (usually a JSON error) is kept for debugging, then we stop
    other = raw_path.with_suffix(".json" if "json" in ctype else ".bin")
    other.write_bytes(r.content)
    try:
        msg = server_messages(r.json())
    except ValueError:
        msg = "response is not JSON"
    raise ErpError(f"Step B did not return an Excel file (saved {other.name}). Server says: {msg}")


# Columns an export may lack because that year's ERP program has a different saved report layout
# (2025-26 has no Branch / Taxableamount / Othercharges; 2021-22..2023-24 also lack BranchInDate /
# DaysFromBranchIn); filled from the JSON details of the same report. Detail keys are matched ignoring case.
DETAILS_ENDPOINT = "acme-inventory-web/inv/getCriteriaStkRptDetails"
FILL_FROM_DETAILS = {"Branch": "branch", "Taxableamount": "taxableamount", "Othercharges": "othercharges",
                     "BranchInDate": "branchindate", "DaysFromBranchIn": "daysfrombranchin", "Scmitem": "scmitem"}
NO_TOTAL = {"Branch", "BranchInDate", "DaysFromBranchIn", "Scmitem"}    # text / date / per-label: no sum on the Total row


def _as_date_text(v):
    """A details date as the export writes it (dd/MM/yyyy); epoch milliseconds are converted."""
    if isinstance(v, (int, float)) and not pd.isna(v) and v > 1e11:
        return pd.Timestamp(v, unit="ms", tz="UTC").tz_convert("Asia/Kolkata").strftime("%d/%m/%Y")
    return v


def fill_from_details(session, filters, report_id, raw_path):
    """If the export lacks only columns in FILL_FROM_DETAILS, add them from the report's JSON rows (matched by
    barcode; every row must match) and rewrite the file, so cleaning and every check run as usual."""
    x = pd.read_excel(raw_path, header=None, dtype=object)
    first = x[0].map(lambda v: v.strip() if isinstance(v, str) else v)
    hits = first.index[first == HEADER_FIRST_CELL]
    if hits.empty:
        return
    hdr = hits[0]
    header = [v.strip() if isinstance(v, str) else None for v in x.loc[hdr]]
    missing = [h for h in EXCEL_TO_DB if h not in header]
    if not missing or set(missing) - FILL_FROM_DETAILS.keys():
        return                                     # complete, or a real layout change: clean_export reports it
    total_idx = next(i for i in x.index[hdr + 1:]
                     if any(isinstance(v, str) and re.match(r"\s*Total \(\d+\)", v) for v in x.loc[i]))
    log.info(f"  Export lacks {missing}; filling them from the report's JSON details...")
    # The stock details endpoint ignores paging and returns every row in one answer
    r = post(session, DETAILS_ENDPOINT, "details", headers=JSON_HEADERS, timeout=600,
             json={"pg": {"offset": 0, "limit": 100000}, "params": {**filters, "reportID": report_id}})
    if r.status_code in (401, 403):
        raise Unauthorized(f"Details: HTTP {r.status_code}")
    rows = ((r.json() or {}).get("data") or {}).get("data") or []
    details = pd.DataFrame(rows).drop_duplicates("barcode").set_index("barcode")
    keys = {str(c).lower(): c for c in details.columns}
    absent = [h for h in missing if FILL_FROM_DETAILS[h] not in keys]
    if absent:
        raise ErpError(f"Could not fill {absent}: not in the details (it has {sorted(keys)[:60]}).")
    barcodes = x.loc[hdr + 1: total_idx - 1, header.index("BarCode/BatchDesc")].map(
        lambda v: v.strip() if isinstance(v, str) else v)
    unmatched = (~barcodes.isin(details.index)).sum()
    if unmatched or len(details) != barcodes.nunique():      # a label can appear twice in older years' exports
        raise ErpError(f"Could not fill {missing}: {unmatched} export rows not in the details "
                       f"({len(details)} details rows, {len(barcodes)} export rows).")
    for h in missing:
        pos = x.shape[1]
        x[pos] = None
        x.loc[hdr, pos] = h
        values = barcodes.map(details[keys[FILL_FROM_DETAILS[h]]])
        if h == "BranchInDate":
            values = values.map(_as_date_text)
        x.loc[barcodes.index, pos] = values
        if h not in NO_TOTAL:
            x.loc[total_idx, pos] = pd.to_numeric(values, errors="coerce").sum()
    x.to_excel(raw_path, header=False, index=False)
    log.info(f"  Filled {missing} for {len(barcodes)} rows from the details.")


def fetch_report(cfg, erp_settings, raw_path, meta_path, run):
    """Login, step A, step B. On 401/403 log in again and retry the steps once."""
    session = new_session(erp_settings)
    for attempt in (1, 2):
        try:
            run.at("login")
            log_code = login(session, erp_settings)
            filters = build_filters(cfg, None if cfg["keep_logcode"] else log_code)
            run.at("step A")
            report_id, record_count = run_step_a(session, cfg, filters, meta_path)
            run.record(report_id=report_id, record_count=record_count)
            run.at("step B")
            run_step_b(session, cfg, filters, report_id, raw_path)
            fill_from_details(session, filters, report_id, raw_path)
            run.record(raw_file=str(rel(raw_path)))
            return
        except Unauthorized as e:
            if attempt == 1:
                log.warning(f"  Not authorised ({e}); logging in again and retrying once...")
                continue
            raise ErpError(f"Still not authorised after re-login ({e}). Check the user's "
                           "ERP rights for this report and ERP_API_KEY.")


# ── Cleaning ─────────────────────────────────────────────────────────────────
def to_number(series):
    """Indian-format text such as '1,15,460.38' -> 115460.38; anything else -> NaN."""
    no_commas = series.map(lambda v: v.replace(",", "") if isinstance(v, str) else v)
    return pd.to_numeric(no_commas, errors="coerce")


def to_date(series):
    """'27/10/2026' -> date; cells Excel already typed as dates pass through."""
    as_text = series.map(lambda v: v.strftime(DATE_FORMAT) if isinstance(v, datetime) else v)
    return pd.to_datetime(as_text, format=DATE_FORMAT, errors="coerce")


def to_text(value):
    """Text cell: strip whitespace; whole numbers lose the '.0' Excel may add."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value.strip() or None
    return None if pd.isna(value) else str(value)


def report_date_for(meta, raw):
    """The as-on date of the report: from meta (new runs) or the 'Printed By' line (old files)."""
    if meta.get("_labelDate"):
        return pd.to_datetime(meta["_labelDate"], format=DATE_FORMAT).date(), "step A meta"
    for cell in raw[0].dropna().astype(str):
        m = re.search(r"Printed By .* On (\d{2}/\d{2}/\d{4})", cell)
        if m:
            return pd.to_datetime(m.group(1), format=DATE_FORMAT).date(), "'Printed By' line"
    raise ErpError("Could not find the report date (no _labelDate in meta, no 'Printed By' line).")


def check(name, passed, detail):
    return {"check": name, "passed": bool(passed), "detail": detail}


def clean_export(raw_xlsx_path, stepA_json_path):
    """Parse the ERP Excel export into DB-ready rows and validate them.

    Returns (df, checks, totals): df uses the erp.stock_snapshot column names,
    checks is a list of {check, passed, detail}, totals is the parsed totals row.
    """
    raw = pd.read_excel(raw_xlsx_path, header=None, dtype=object)
    meta = json.loads(Path(stepA_json_path).read_text(encoding="utf-8"))

    # Header row = first row whose column 0 is "Sr.No"; the title rows above it go
    first_col = raw[0].map(lambda v: v.strip() if isinstance(v, str) else v)
    hits = first_col.index[first_col == HEADER_FIRST_CELL]
    if hits.empty:
        raise ErpError(f"No header row starting with '{HEADER_FIRST_CELL}' in {raw_xlsx_path}")
    hdr = hits[0]

    # Data ends before the "Total (N)" row; it and the "Printed By" row are dropped
    def is_total_row(row):
        return any(isinstance(v, str) and re.match(r"\s*Total \(\d+\)", v) for v in row)
    total_rows = [i for i in raw.index[hdr + 1:] if is_total_row(raw.loc[i])]
    if not total_rows:
        raise ErpError("No 'Total (N)' row found below the header.")
    total_idx = total_rows[0]

    header = [v.strip() if isinstance(v, str) else None for v in raw.loc[hdr]]
    data = raw.loc[hdr + 1: total_idx - 1]
    total_cells = raw.loc[total_idx]
    total_label = next(v for v in total_cells if isinstance(v, str) and "Total (" in v)
    total_n = int(re.search(r"\((\d+)\)", total_label).group(1))
    log.info(f"Header at row {hdr}, {len(data)} data rows, totals row '{total_label.strip()}'")

    # Match the header to EXCEL_TO_DB; columns without a header must be empty
    unknown = [h for h in header if h and h not in EXCEL_TO_DB]
    missing = [h for h in EXCEL_TO_DB if h not in header]
    if unknown or missing:
        raise ErpError(f"Export layout changed. Unknown columns: {unknown}; missing: {missing}. "
                       "Update EXCEL_TO_DB (and sql/schema.sql).")
    for pos, h in enumerate(header):
        if h is None and data[pos].notna().any():
            raise ErpError(f"Column {pos} has no header but holds data.")

    # Build the clean table, column by column, aligned by position.
    # The totals row goes through the same number parsing.
    df = pd.DataFrame(index=range(len(data)))
    totals = {}
    lost = {}                       # values that failed to parse -> validation
    for pos, h in enumerate(header):
        if h is None:
            continue
        col, cells = EXCEL_TO_DB[h], data[pos].reset_index(drop=True)
        cells = cells.map(lambda v: v.strip() if isinstance(v, str) else v).replace("", None)
        if col in INT_COLUMNS or col in DECIMAL_COLUMNS:
            values = to_number(cells)
            if col in INT_COLUMNS:
                values = values.where(values % 1 == 0)        # 1.5 in an int column -> lost
                values = values.astype("Int64")
            totals[col] = to_number(pd.Series([total_cells[pos]])).iloc[0]
        elif col in DATE_COLUMNS:
            values = to_date(cells)
        else:
            values = cells.map(to_text)
        lost[col] = int(cells.notna().sum() - values.notna().sum())
        df[col] = values

    snapshot_date, source = report_date_for(meta, raw)
    report_id = (meta.get("data") or {}).get("reportID")
    record_count = ((meta.get("data") or {}).get("serverTotal") or {}).get("recordCount")
    df.insert(0, "snapshot_date", snapshot_date)
    df["report_id"] = report_id
    log.info(f"snapshot_date = {snapshot_date:%d/%m/%Y} (from {source}), report_id = {report_id}")

    # ── Validation ──
    rows = len(df)
    max_sr = df["sr_no"].max()
    checks = [
        check("Row count == max(sr_no)", rows == max_sr, f"{rows} vs {max_sr}"),
        check("Row count == Total (N)", rows == total_n, f"{rows} vs {total_n}"),
        check("Row count == step A recordCount", rows == record_count, f"{rows} vs {record_count}"),
    ]
    for col in SUM_COLUMNS:
        ours, theirs = float(df[col].sum()), totals.get(col)
        ok = theirs is not None and pd.notna(theirs) and abs(ours - theirs) <= SUM_TOLERANCE
        checks.append(check(f"Sum {col} == totals row", ok, f"{ours:,.3f} vs {theirs:,.3f}"))
    nulls = int(df["barcode"].isna().sum())
    checks.append(check("No null barcode", nulls == 0, f"{nulls} null"))
    # Older years' exports give each piece of a set the set's code (121-A4212 on its pendant, tops and chain):
    # those pieces get set code + "/" + label no, so every row has its own barcode.
    shared = df["barcode"].notna() & df.duplicated("barcode", keep=False)
    if shared.any():
        df.loc[shared, "barcode"] = df.loc[shared, "barcode"] + "/" + df.loc[shared, "label_no"].astype(str)
        log.info(f"  {int(shared.sum())} set pieces shared their set's code; gave each code/label no.")
    dups = int(df["barcode"].dropna().duplicated().sum())
    checks.append(check("One row per barcode", dups == 0, f"{dups} duplicated"))
    # Older years hold typing errors such as a wastage of 400,991 %: blanked (the app does not use it) rather
    # than rejecting the date; numeric(8,3) cannot store them anyway.
    if "wastage_per" in df:
        wild = df["wastage_per"].abs() >= 1000
        if wild.any():
            log.info(f"  {int(wild.sum())} labels with an impossible wastage % "
                     f"({', '.join(f'{v:,.0f}' for v in df.loc[wild, 'wastage_per'].head(3))}) left blank.")
            df.loc[wild, "wastage_per"] = None
    bad = {c: n for c, n in lost.items() if n}
    checks.append(check("No unparseable numbers/dates", not bad, str(bad) if bad else "0 lost"))
    return df, checks, {"count": total_n, **totals}


def breakdown(series, top=5):
    """'KJ2 / Branch Dharampeth: 17, Whole Sale Branch: 2, ...' (largest first)."""
    counts = series.fillna("(blank)").astype(str).value_counts()
    text = ", ".join(f"{k}: {v}" for k, v in counts.head(top).items())
    return text + (f", +{len(counts) - top} more" if len(counts) > top else "")


def data_quality(df):
    """Warnings about the cleaned data. Each has a count (stored every run, for
    trends) and a short detail. They are reported, never used to block a load."""
    no_date = df["labeling_date"].isna()
    customer = df["item_class_group"] == CUSTOMER_GROUP
    zero_value = (df["item_amt"] == 0) & ~customer
    purity = df["purity"]
    odd_purity = ~((purity == 0) | purity.between(KNOWN_PURITY_MIN, KNOWN_PURITY_MAX))
    return {
        "null_labeling_date": {
            "count": int(no_date.sum()),
            "detail": breakdown(df.loc[no_date, "branch"])},
        "zero_item_amt_not_customer": {
            "count": int(zero_value.sum()),
            "detail": breakdown(df.loc[zero_value, "item_class_group"])},
        "customer_items": {
            "count": int(customer.sum()),
            "detail": breakdown(df.loc[customer, "branch"])},
        "purity_outside_karat_ranges": {
            "count": int(odd_purity.sum()),
            "detail": breakdown(purity[odd_purity])},
    }
