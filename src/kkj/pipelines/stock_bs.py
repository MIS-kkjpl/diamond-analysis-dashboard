"""
Stock in Business Studio's 7 categories: several ERP pulls -> one day in erp.stock_snapshot_bs.

Each pull in config/stock_categories.json is fetched (step A + step B, same report as the nightly stock load),
cleaned and checked on its own; then rows get their category (kkj.erp.stock_categories) and the whole day is
loaded in one transaction and read back per category. Any failed pull or check: nothing is loaded.
Never touches erp.stock_snapshot or the nightly job.

Run (from the project folder):
  python -m kkj.pipelines.stock_bs                    # today: fetch every pull, check, load
  python -m kkj.pipelines.stock_bs --no-load          # fetch + check only
  python -m kkj.pipelines.stock_bs --from-stamp 20261121_1600 [--no-load]   # reuse that run's saved files
"""
import argparse
import logging
import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from kkj import settings
from kkj.db import stock as db
from kkj.erp.client import ErpError
from kkj.erp.stock_categories import CATEGORIES, assign, load_config
from kkj.erp.stock_report import (EXPORT_ENDPOINT, REPORT_NAME, RUN_ENDPOINT, SUM_TOLERANCE, clean_export,
                                  fetch_report, rel)
from kkj.pipelines.stock import LocalRun, log_checks

TABLE = "erp.stock_snapshot_bs"
# Batch stock (loose stones) repeats a barcode across locations; uniqueness is restored after categorising
CHECKS_FIXED_LATER = {"One row per barcode"}

log = logging.getLogger("kkj")


def setup_logging(stamp):
    """Console + logs/stock_bs_<stamp>.log (apart from the nightly job's run_<stamp>.log)."""
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOGS_DIR / f"stock_bs_{stamp}.log"
    log.setLevel(logging.INFO)
    for handler, fmt in ((logging.StreamHandler(sys.stdout), "%(message)s"),
                         (logging.FileHandler(log_path, encoding="utf-8"), "%(asctime)s %(levelname)-7s %(message)s")):
        handler.setFormatter(logging.Formatter(fmt))
        log.addHandler(handler)
    return log_path


def files_for(pull, stamp):
    name = f"{REPORT_NAME}_bs_{pull}"
    return (settings.STOCK_RAW_DIR / f"{name}_{stamp}.xlsx",
            settings.STOCK_META_DIR / f"{name}_{stamp}_stepA.json")


def fetch(pull, date, stamp, erp_settings):
    raw, meta = files_for(pull["name"], stamp)
    cfg = {"name": pull["name"], "run_endpoint": RUN_ENDPOINT, "export_endpoint": EXPORT_ENDPOINT,
           "locations": [], "products": pull["products"], "items": [], "date": date, "keep_logcode": False,
           "refresh_item_codes": True, "item_attributes": pull.get("item_attributes", [])}
    fetch_report(cfg, erp_settings, raw, meta, LocalRun())


def clean(pull, stamp):
    raw, meta = files_for(pull["name"], stamp)
    if not raw.exists():
        raise ErpError(f"Missing {rel(raw)}")
    df, checks, totals = clean_export(raw, meta)
    log_checks(checks, f"Validation: {pull['name']}")
    failed = [c["check"] for c in checks if not c["passed"] and c["check"] not in CHECKS_FIXED_LATER]
    if failed:
        raise ErpError(f"{pull['name']}: validation failed ({', '.join(failed)})")
    return df


def read_back(snapshot_date):
    with db.get_engine().connect() as conn:
        return pd.read_sql(text(f"""SELECT stock_category, count(*) AS rows, sum(stock_value)::float AS stock_value
                                    FROM {TABLE} WHERE snapshot_date = :d GROUP BY 1"""),
                           conn, params={"d": snapshot_date}).set_index("stock_category")


def run(stamp, fetch_first, load):
    cfg = load_config()
    date = datetime.strptime(stamp, "%Y%m%d_%H%M").strftime("%d/%m/%Y")
    if fetch_first:
        erp_settings = settings.erp_login_settings()
        for pull in cfg["pulls"]:
            log.info(f"── Pull {pull['name']}: groups {pull['products']}")
            fetch(pull, date, stamp, erp_settings)
    frames = {p["name"]: clean(p, stamp) for p in cfg["pulls"]}
    dates = {n: f["snapshot_date"].iloc[0] for n, f in frames.items()}
    if len(set(dates.values())) != 1:
        raise ErpError(f"Pulls are for different days: {dates}")

    df, left_out = assign(frames, cfg)
    snapshot_date = df["snapshot_date"].iloc[0]
    dups = int(df["barcode"].duplicated().sum())
    if dups:
        raise ErpError(f"{dups} barcodes still repeat after categorising")
    summary = df.groupby("stock_category").agg(rows=("barcode", "size"), pcs=("pcs", "sum"),
                                               stock_value=("stock_value", "sum")).reindex(CATEGORIES).fillna(0)
    log.info(f"Stock for {snapshot_date:%d/%m/%Y} in Business Studio's categories:\n"
             + summary.to_string(formatters={"stock_value": "{:,.2f}".format})
             + f"\nTotal value (Item Amt + Other Charges) {df['stock_value'].sum():,.2f}; left out: {left_out}")
    if not load:
        log.info("--no-load: checked only, nothing written to the database.")
        return True

    rows = db.load_snapshot(df, snapshot_date, None, table=TABLE)
    stored = read_back(snapshot_date)
    ok = True
    for cat, r in summary[summary["rows"] > 0].iterrows():
        got = stored.loc[cat] if cat in stored.index else None
        same = (got is not None and int(got["rows"]) == int(r["rows"])
                and abs(float(got["stock_value"]) - float(r["stock_value"])) <= SUM_TOLERANCE)
        ok &= same
        if not same:
            log.error(f"  Read-back mismatch for {cat}: {None if got is None else got.to_dict()} vs {r.to_dict()}")
    log.info(f"Loaded {rows} rows into {TABLE} for {snapshot_date:%d/%m/%Y}; read back "
             f"{'OK' if ok else 'MISMATCH'}.")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stock in Business Studio's 7 categories -> erp.stock_snapshot_bs.")
    ap.add_argument("--from-stamp", help="reuse the saved files of that run (YYYYMMDD_HHMM); no ERP calls")
    ap.add_argument("--no-load", action="store_true", help="fetch/clean + check only; no database writes")
    args = ap.parse_args(argv)
    stamp = args.from_stamp or datetime.now().strftime("%Y%m%d_%H%M")
    log_path = setup_logging(stamp)
    try:
        ok = run(stamp, fetch_first=not args.from_stamp, load=not args.no_load)
    except (ErpError, settings.ConfigError, ValueError) as e:
        log.error(f"ERROR: {e}. Nothing loaded.")
        ok = False
    log.info(f"Log: {rel(log_path)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
