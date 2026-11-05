"""
Criteria-wise Sales report: fetch from the ERP, clean the rows, validate.

The ERP builds this report in steps, like the browser does:
  Step A   getRptKeyAndServerTotalForSalesRpt  runs the report on the server and
           returns a reportID plus serverTotal (recordCount, pcs, itemamt, ...).
  Details  getCriteriaStkSalesRptDetails       pages through the rows as JSON
           (97 fields a row, incl. salesman, taxable amount, inventory ids).
Details only work for a reportID the server issued in step A; a made-up reportID
gives "Records Not Found". Rows are taken from the JSON pages (richer than the
Excel export) and the whole response is saved before anything is cleaned, so a
date range never has to be pulled twice.

Returns (items brought back to the store) need considerSalesReturn "2" (0 and 1 leave them
out; verified 2026-11-04). They come as bill type SR with negative pcs/weights/item_amt, the
ERP's salesReturnDoneFlag set, and NO barcode, label no or taxable amount. A return carries
the same inventoryRefenceCode as the sale it reverses (itemInvOwnCode does NOT link them).
The report selects lines by the ORIGINAL sale's date: a return appears in the pull covering
its sale, dated on the day it came back (so possibly after the range). orig_sale_date and
orig_bill_no record that sale; loads replace rows by orig_sale_date.
No database code here (see kkj.db.history); the pipeline is kkj.pipelines.sales.
"""
import gzip
import json
import logging
import math
import time

import pandas as pd

from kkj import settings
from kkj.erp.client import (JSON_HEADERS, ErpError, Unauthorized, item_codes_for_groups, post,
                            server_messages)
from kkj.erp.stock_report import check, rel

log = logging.getLogger("kkj.sales")

RUN_ENDPOINT = "acme-inventory-web/inv/getRptKeyAndServerTotalForSalesRpt"
DETAILS_ENDPOINT = "acme-inventory-web/inv/getCriteriaStkSalesRptDetails"
PAGE_SIZE = 2000
SUM_TOLERANCE = 0.01

# Step A fields the details call also needs (the rest of its body is fixed, below)
_DETAIL_KEYS = ("flagForLocation", "locationCodeList", "productCodeList", "imageView",
                "selectedStoneAttr", "selectedAttr", "selectdExtraCharges", "flagForItem",
                "itemCodeList", "flagForOneStone", "attrWiseRateVarience", "lblRptDiaRate",
                "ftpMasterCode", "storeImgOn", "storeOnFtp", "supEntrySetting", "logCode",
                "reportParam", "amountRangeFilter", "fromAmount", "toAmount", "piecesFilter",
                "fromPieces", "toPieces", "salesmanCodeList", "flagForSalesman", "onlySetItems",
                "subReportID", "allItemCodes", "viewSupplier", "fromDate", "toDate", "salesType",
                "considerSalesReturn")

# JSON field -> sales.sales_line column
FIELDS = {
    "date": "sale_date", "billno": "bill_no", "docno": "doc_no", "lblno": "label_no",
    "barcode": "barcode", "setitembarcode": "set_item_barcode", "itemname": "item_name",
    "category": "category", "itemsubcategory": "sub_category", "groupname": "item_class_group",
    "branch": "branch", "location": "location", "salesman": "salesman",
    "suppliername": "supplier", "purity": "purity", "pcs": "pcs", "grwt": "gross_wt",
    "netwt": "net_wt", "finewt": "fine_wt", "diapcs": "dia_pcs", "diawt": "dia_wt",
    "cstwt": "cst_wt", "studdedwt": "stone_wt", "diaamt": "dia_amt", "studdedamt": "stone_amt",
    "cstamt": "cst_amt", "labourrate": "labour_rate", "labourcharges": "labour_charges",
    "wastageper": "wastage_per", "wastageamount": "wastage_amount", "rate": "rate",
    "itemamt": "item_amt", "taxableamount": "taxable_amount", "othercharges": "other_charges",
    "daysfromlabeling": "age_days_at_sale", "daysfrombranchin": "days_from_branch_in",
    "orglbldate": "labeling_date", "lbldt": "label_dt", "branchindate": "branch_in_date",
    "latesttransactiondate": "latest_txn_date", "orderno": "order_no",
    "labourtype": "labour_type", "itemCode": "item_code", "batchCode": "batch_code",
    "itemInvOwnCode": "item_inv_own_code", "inventoryRefenceCode": "inventory_ref_code",
    "salesReturnDoneFlag": "sales_return_done",
}
INT_COLUMNS = ["label_no", "dia_pcs", "age_days_at_sale", "days_from_branch_in", "item_code",
               "batch_code", "item_inv_own_code", "inventory_ref_code"]
DECIMAL_COLUMNS = ["purity", "pcs", "gross_wt", "net_wt", "fine_wt", "dia_wt", "cst_wt",
                   "stone_wt", "dia_amt", "stone_amt", "cst_amt", "labour_rate", "labour_charges",
                   "wastage_per", "wastage_amount", "rate", "item_amt", "taxable_amount",
                   "other_charges"]
DATE_COLUMNS = ["sale_date", "labeling_date", "label_dt", "branch_in_date", "latest_txn_date"]
DATE_FORMAT = "%d/%m/%Y"
# serverTotal field -> our column, compared after cleaning
TOTALS = {"pcs": "pcs", "itemamt": "item_amt", "netwt": "net_wt", "diawt": "dia_wt"}


def build_params(cfg, item_codes, log_code=None):
    """sales_payload.json's params for one date range, all diamond groups."""
    p = json.loads((settings.CONFIG_DIR / "sales_payload.json").read_text(encoding="utf-8"))["params"]
    # showOrderItem: "2" (Show Order Labels: No, the captured screen value) leaves out every sale of a piece
    # booked against a customer order (June 2026: 52 of 577 lines); "1" includes them, "3" is only them
    p.update(productCodeList=cfg["products"], allItemCodes=item_codes,
             fromDate=cfg["from"], toDate=cfg["to"],
             salesType=cfg["sales_type"], considerSalesReturn=cfg["sales_return"],
             showOrderItem=cfg.get("order_labels", "1"),
             filterStr=[f" Date From: {cfg['from']} To: {cfg['to']} Location:All  Product Group:Diamond"
                        "      Show Labels In:Both  Show Order Labels:Yes ", "Items:All         "])
    if log_code:
        p["logCode"] = log_code
    return p


def details_body(params, report_id, offset, limit):
    body = {k: params[k] for k in _DETAIL_KEYS}
    body.update(reportID=report_id, flagForPrev=False, flagForNext=False, searchString="",
                flagForSearch=False, stoneCode="", stoneCodeList=[], exposeFTPToHTTP=False,
                httpServerUrl="", noOfImages=0, sortedBy=[], selectedImageType="1",
                selectCurPageImg=False, selectAllPageImg=False, labelDateFrom="", labelDateTo="")
    return {"pg": {"offset": offset, "limit": limit}, "params": body}


def _json(r, what):
    if r.status_code in (401, 403):
        raise Unauthorized(f"{what}: HTTP {r.status_code}")
    if not r.ok:
        raise ErpError(f"{what} failed: HTTP {r.status_code}.")
    try:
        return r.json()
    except ValueError:
        raise ErpError(f"{what}: response is not JSON.")


def fetch(session, cfg, log_code, raw_path, item_codes=None):
    """Step A + every details page; save {params, stepA, rows} to raw_path (.json.gz).

    Returns the saved dict. item_codes: pass a cached list to skip the lookups.
    """
    item_codes = item_codes or item_codes_for_groups(session, cfg["products"])
    params = build_params(cfg, item_codes, log_code)

    log.info(f"Step A: sales {cfg['from']} to {cfg['to']} (salesType {cfg['sales_type']}, "
             f"considerSalesReturn {cfg['sales_return']}), {len(item_codes)} item codes...")
    t0 = time.time()
    step_a = _json(post(session, RUN_ENDPOINT, "step A (run report)", json={"params": params},
                        headers=JSON_HEADERS), "Step A")
    inner = step_a.get("data") if isinstance(step_a.get("data"), dict) else {}
    report_id, totals = inner.get("reportID"), inner.get("serverTotal") or {}
    log.info(f"  {time.time() - t0:.1f} s: reportID {report_id}, recordCount {totals.get('recordCount')}")
    rows = []
    if not report_id:
        msg = server_messages(step_a)
        if "Records Not Found" not in msg:
            raise ErpError(f"Step A returned no reportID. Server says: {msg}")
        log.info("  No sales in this range.")
    else:
        t0, offset = time.time(), 0
        while True:
            page = _json(post(session, DETAILS_ENDPOINT, "details",
                              json=details_body(params, report_id, offset, PAGE_SIZE),
                              headers=JSON_HEADERS), "Details")
            got = ((page.get("data") or {}).get("data")) or []
            rows += got
            offset += PAGE_SIZE
            if len(got) < PAGE_SIZE:
                break
        log.info(f"  Details: {len(rows)} rows in {time.time() - t0:.1f} s")

    saved = {"params": params, "stepA": step_a, "rows": rows}
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw_path, "wt", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False)
    log.info(f"  Saved: {rel(raw_path)}")
    return saved


def load_raw(raw_path):
    with gzip.open(raw_path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _text(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):   # pandas fills absent JSON keys with NaN
        return None
    v = str(v).strip()
    return v or None


def clean(saved):
    """Saved response -> (df with sales.sales_line columns, checks, server totals)."""
    rows = saved["rows"]
    inner = saved["stepA"].get("data") if isinstance(saved["stepA"].get("data"), dict) else {}
    totals = inner.get("serverTotal") or {}
    src = pd.DataFrame(rows)
    df, lost = pd.DataFrame(index=range(len(rows))), {}
    for field, col in FIELDS.items():
        cells = src[field] if field in src else pd.Series([None] * len(rows))
        cells = cells.map(_text)
        if col in INT_COLUMNS or col in DECIMAL_COLUMNS:
            values = pd.to_numeric(cells.str.replace(",", "", regex=False), errors="coerce")
            if col in INT_COLUMNS:
                values = values.where(values % 1 == 0).astype("Int64")
        elif col in DATE_COLUMNS:
            values = pd.to_datetime(cells, format=DATE_FORMAT, errors="coerce").dt.date
        elif col == "sales_return_done":
            values = cells.map(lambda v: None if v is None else v.lower() == "true")
        else:
            values = cells
        lost[col] = int(cells.notna().sum() - pd.Series(values).notna().sum())
        df[col] = values
    df["raw"] = [json.dumps(r, ensure_ascii=False) for r in rows]
    df["barcode_rebuilt"] = False

    # Link each return to the sale it reverses (same inventory_ref_code, in this same pull)
    p = saved["params"]
    range_from = pd.to_datetime(p["fromDate"], format=DATE_FORMAT).date()
    range_to = pd.to_datetime(p["toDate"], format=DATE_FORMAT).date()
    is_return = df["pcs"] < 0
    sale_lines = df[~is_return].dropna(subset=["inventory_ref_code"]).drop_duplicates("inventory_ref_code")
    by_ref = sale_lines.set_index("inventory_ref_code")
    ref = df["inventory_ref_code"]
    df["orig_sale_date"] = df["sale_date"].where(~is_return, ref.map(by_ref["sale_date"]))
    df["orig_bill_no"] = df["bill_no"].where(~is_return, ref.map(by_ref["bill_no"]))
    unlinked = is_return & df["orig_sale_date"].isna()
    df.loc[unlinked, "orig_sale_date"] = range_from          # still inside this pull's scope

    n = len(df)
    outside = int((~df.loc[~is_return, "sale_date"].between(range_from, range_to)).sum())
    dup = int(df.duplicated(["bill_no", "inventory_ref_code"]).sum())
    checks = [check("Rows == step A recordCount", n == (totals.get("recordCount") or 0),
                    f"{n} vs {totals.get('recordCount')}"),
              check("Sale lines dated inside the range", outside == 0, f"{outside} outside"),
              check("One row per bill + inventory_ref_code",
                    dup == 0 and df["inventory_ref_code"].notna().all(),
                    f"{dup} duplicated, {int(df['inventory_ref_code'].isna().sum())} null")]
    for field, col in TOTALS.items():
        ours, theirs = float(df[col].sum()) if n else 0.0, totals.get(field)
        ok = (theirs is None and n == 0) or (theirs is not None and abs(ours - float(theirs)) <= SUM_TOLERANCE)
        checks.append(check(f"Sum {col} == serverTotal.{field}", ok, f"{ours:,.3f} vs {theirs}"))
    checks += [
        check("Every sale date parsed", df["sale_date"].notna().all(), f"{int(df['sale_date'].isna().sum())} unparsed"),
        check("No null bill no; no null barcode except on returns",
              df["bill_no"].notna().all() and df.loc[df["pcs"] >= 0, "barcode"].notna().all(),
              f"{int(df['bill_no'].isna().sum())} / {int(df.loc[df['pcs'] >= 0, 'barcode'].isna().sum())} null"),
        check("No unparseable numbers/dates", not any(lost.values()),
              str({c: k for c, k in lost.items() if k}) if any(lost.values()) else "0 lost"),
    ]
    return df, checks, totals


def data_quality(df):
    """Informational counts (never block a load)."""
    returns = df["pcs"] < 0
    return {
        "return_lines": int(returns.sum()),
        "return_item_amt": float(df.loc[returns, "item_amt"].sum()),
        "returns_without_taxable_amount": int(df.loc[returns, "taxable_amount"].isna().sum()),
        "returns_not_linked_to_sale": int((returns & df["orig_bill_no"].isna()).sum()),
        "returns_on_a_later_day": int((returns & (df["sale_date"] != df["orig_sale_date"])).sum()),
        "sales_return_done_flag": int(df["sales_return_done"].fillna(False).sum()),
        "bill_types": df["bill_no"].str.extract(r"(SSB|SOS|SR|SB)-\d+$")[0].value_counts().to_dict(),
        "by_group": df["item_class_group"].value_counts().to_dict(),
        "taxable_over_item_amt": round(float(df["taxable_amount"].sum() / df["item_amt"].sum()), 4)
        if len(df) and df["item_amt"].sum() else None,
    }

