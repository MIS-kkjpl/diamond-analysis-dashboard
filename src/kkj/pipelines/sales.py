"""
Sales pipeline: Criteria-wise Sales report, Acme Padm ERP -> PostgreSQL (sales.sales_line).

Each date range (one month at a time with --by-month) is: step A -> details pages
-> save data/sales/raw/<range>_<stamp>.json.gz -> clean -> validate against the
ERP's serverTotal -> replace that range's ERP rows in ONE transaction -> read
back and compare. Nothing is loaded unless every check passes; every load is
recorded in sales.load_log.

Run (from the project folder):
  python -m kkj.pipelines.sales --from 01/10/2026 --to 31/10/2026
  python -m kkj.pipelines.sales --from 01/04/2026 --to 04/11/2026 --by-month --pause 20
  python -m kkj.pipelines.sales --from 30/06/2026 --to 30/06/2026 --no-load   # fetch + check only
  python -m kkj.pipelines.sales --from-file data/sales/raw/<file>.json.gz      # no ERP
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from kkj import settings
from kkj.db import history as db
from kkj.erp.client import ErpError, Unauthorized, login, new_session, item_codes_for_groups
from kkj.erp.sales_report import clean, data_quality, fetch, load_raw
from kkj.erp.stock_report import PRODUCT_CODES, check, rel

SALES_RAW_DIR = settings.DATA_DIR / "sales" / "raw"
SOURCE = "erp"
DATE_FORMAT = "%d/%m/%Y"

log = logging.getLogger("kkj")


def setup_logging(stamp):
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOGS_DIR / f"sales_{stamp}.log"
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logfile = logging.FileHandler(log_path, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(console)
    log.addHandler(logfile)
    return log_path


def log_checks(checks, title):
    log.info(f"{title}:")
    for c in checks:
        log.info(f"  {'PASS' if c['passed'] else 'FAIL'}  {c['check']}  ({c['detail']})")


def month_chunks(start, end):
    """[(from, to)] covering start..end, split at month ends."""
    chunks, cur = [], start
    while cur <= end:
        month_end = (pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date()
        chunks.append((cur, min(month_end, end)))
        cur = (pd.Timestamp(month_end) + pd.Timedelta(days=1)).date()
    return chunks


def process(saved, raw_path, no_load):
    """Clean + validate one saved range; load it unless no_load. Returns True on success."""
    p = saved["params"]
    date_from = datetime.strptime(p["fromDate"], DATE_FORMAT).date()
    date_to = datetime.strptime(p["toDate"], DATE_FORMAT).date()
    df, checks, totals = clean(saved)
    log_checks(checks, f"Validation {date_from} to {date_to} ({len(df)} rows)")
    log.info(f"Data quality: {data_quality(df) if len(df) else 'no rows'}")
    if not all(c["passed"] for c in checks):
        log.error("Validation failed. Nothing loaded.")
        return False
    if no_load:
        log.info("--no-load: checked only, nothing written to the database.")
        return True

    report_id = (saved["stepA"].get("data") or {}).get("reportID") if isinstance(saved["stepA"].get("data"), dict) else None
    load_id = db.start_sales_load(SOURCE, date_from, date_to, raw_file=str(rel(raw_path)),
                                  report_id=report_id, record_count=totals.get("recordCount") or 0)
    try:
        rows = db.replace_sales(df, SOURCE, date_from, date_to, load_id)
        stored = db.load_totals(load_id)
        pcs, amt = float(df["pcs"].sum()) if rows else 0.0, float(df["item_amt"].sum()) if rows else 0.0
        db_checks = [
            check("DB count == rows", stored["count"] == len(df), f"{stored['count']} vs {len(df)}"),
            check("DB sum pcs == rows", abs(stored["pcs"] - pcs) <= 0.01, f"{stored['pcs']:,.3f} vs {pcs:,.3f}"),
            check("DB sum item_amt == rows", abs(stored["item_amt"] - amt) <= 0.01,
                  f"{stored['item_amt']:,.2f} vs {amt:,.2f}"),
        ]
        log_checks(db_checks, "Database check")
        ok = all(c["passed"] for c in db_checks)
        db.finish_sales_load(load_id, "success" if ok else "failed", rows, checks + db_checks,
                             dq_warnings=data_quality(df) if rows else None,
                             error=None if ok else "database check failed")
        log.info(f"Load {load_id}: {rows} rows, {'success' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        db.finish_sales_load(load_id, "failed", 0, checks, error=f"{type(e).__name__}: {e}"[:2000])
        raise


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Criteria-wise Sales: ERP -> PostgreSQL (sales.sales_line).")
    ap.add_argument("--from", dest="date_from", help="first sale date dd/mm/yyyy")
    ap.add_argument("--to", dest="date_to", help="last sale date dd/mm/yyyy (default: --from)")
    ap.add_argument("--by-month", action="store_true", help="one ERP report per calendar month")
    ap.add_argument("--pause", type=float, default=5, help="seconds to wait between reports")
    ap.add_argument("--products", default=",".join(map(str, PRODUCT_CODES)),
                    help="comma-separated product group codes")
    ap.add_argument("--sales-type", default="1", help="ERP salesType (captured value: 1)")
    ap.add_argument("--sales-return", default="2",
                    help="ERP considerSalesReturn: 2 includes returns as negative rows (0/1 leave them out)")
    ap.add_argument("--no-load", action="store_true", help="fetch + check only; no database writes")
    ap.add_argument("--order-labels", default="1", choices=("1", "2", "3"),
                    help="ERP showOrderItem: 1 all sales (default), 2 without order-booked pieces, 3 ONLY order-booked "
                         "pieces (implies --no-load; used to mark them, see kkj.pipelines.order_booked)")
    ap.add_argument("--from-file", type=Path, help="clean + check (+ load) a saved .json.gz; no ERP")
    args = ap.parse_args(argv)
    if not args.from_file and not args.date_from:
        ap.error("give --from (and optionally --to), or --from-file")
    return args


def main(argv=None):
    args = parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = setup_logging(stamp)

    if args.from_file:
        ok = process(load_raw(args.from_file), args.from_file, args.no_load)
        log.info(f"Log: {rel(log_path)}")
        sys.exit(0 if ok else 1)

    start = datetime.strptime(args.date_from, DATE_FORMAT).date()
    end = datetime.strptime(args.date_to or args.date_from, DATE_FORMAT).date()
    chunks = month_chunks(start, end) if args.by_month else [(start, end)]
    products = [int(x) for x in args.products.split(",") if x.strip()]

    erp_settings = settings.erp_login_settings()
    session = new_session(erp_settings)
    log_code = login(session, erp_settings)
    item_codes = item_codes_for_groups(session, products)
    log.info(f"{len(chunks)} report(s); {len(item_codes)} item codes in groups {products}")

    results = []
    for i, (a, b) in enumerate(chunks):
        if i:
            time.sleep(args.pause)
        cfg = {"products": products, "from": a.strftime(DATE_FORMAT), "to": b.strftime(DATE_FORMAT),
               "sales_type": args.sales_type, "sales_return": args.sales_return, "order_labels": args.order_labels}
        tag = "" if args.order_labels == "1" else f"o{args.order_labels}"
        raw_path = SALES_RAW_DIR / f"sales_{a:%Y%m%d}_{b:%Y%m%d}_t{args.sales_type}r{args.sales_return}{tag}_{stamp}.json.gz"
        try:
            try:
                saved = fetch(session, cfg, log_code, raw_path, item_codes)
            except Unauthorized:
                log.warning("  Not authorised; logging in again and retrying once...")
                log_code = login(session, erp_settings)
                saved = fetch(session, cfg, log_code, raw_path, item_codes)
            results.append(process(saved, raw_path, args.no_load or args.order_labels != "1"))
        except (ErpError, settings.ConfigError) as e:
            log.error(f"{a} to {b}: {e}")
            results.append(False)
    log.info(f"{sum(results)}/{len(results)} range(s) OK. Log: {rel(log_path)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
