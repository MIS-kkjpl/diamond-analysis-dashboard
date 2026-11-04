"""
Stock pipeline: Item-Wise Current Stock, Acme Padm ERP -> PostgreSQL (erp.stock_snapshot).

Normal run:   login -> step A -> step B -> save raw/ + meta/ -> clean -> validate -> load
--from-file:  skip the ERP; clean -> validate -> load an Excel file in data/stock/raw/.
--check-only: with --from-file, clean + validate only; no database, no ERP (safe test).

Every real run is recorded in erp.etl_run_log from its first moment (status
'running'). If any step fails (login, step A, step B, cleaning, validation,
loading) the run is recorded as 'failed' with "<step>: <error>" and the script
exits with code 1; nothing is loaded unless every validation check passes.
Re-running a day replaces that day's rows.

Run (from the project folder, see README):
  python -m kkj.pipelines.stock
  python -m kkj.pipelines.stock --locations 1,2 --date 27/10/2026
  python -m kkj.pipelines.stock --from-file data/stock/raw/itemwise_current_stock_20261028_1328.xlsx
  python -m kkj.pipelines.stock --from-file <file> --check-only
  python -m kkj.pipelines.stock --dry-run       # show both request bodies' changes, no ERP call

Ported from archive/inventory_project/Phase_one.py; behaviour is unchanged
except the new --check-only mode and the data/ + logs/ paths.
"""
import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from kkj import settings
from kkj.db import stock as db
from kkj.erp.client import HOST, SPROGRAM_KEY, ErpError
from kkj.erp.stock_report import (EXPORT_ENDPOINT, ITEM_CODES, LOCATION_CODES, PRODUCT_CODES,
                                  REPORT_NAME, RUN_ENDPOINT, STEP_B_ONLY, SUM_TOLERANCE,
                                  build_filters, check, clean_export, data_quality, fetch_report,
                                  rel, step_a_body, step_b_body)

LATEST_NAME = "stock_latest"         # data/stock/clean/stock_latest.parquet

log = logging.getLogger("kkj")


class RunTracker:
    """This run's row in erp.etl_run_log, plus the step currently running.

    Created before anything else, so started_at/finished_at cover the whole run.
    Each stage calls at("<step>"); a failure is recorded as "<step>: <error>".
    """

    def __init__(self, source):
        # Earlier runs that were killed before recording a result
        stale = db.close_stale_runs()
        if stale:
            log.warning(f"Marked {stale} interrupted run(s) (still 'running' after "
                        f"{db.STALE_AFTER}) as failed.")
        else:
            log.info("No interrupted runs to clean up.")
        self.run_id = db.start_run(source)
        self.step = "start"
        self.checks = []          # every validation result so far
        self.rows_loaded = 0
        self.dq_warnings = None   # set once the data is cleaned
        log.info(f"Run {self.run_id} started (source={source}).")

    def at(self, step):
        self.step = step

    def record(self, **details):
        """Fill in report_id / raw_file / record_count as they become known."""
        db.update_run(self.run_id, **details)

    def succeed(self):
        db.finish_run(self.run_id, "success", self.rows_loaded, self.checks,
                      dq_warnings=self.dq_warnings)
        log.info(f"Run {self.run_id} finished: success.")

    def fail(self, message):
        error = f"{self.step}: {message}"
        log.error(f"Run {self.run_id} failed at {error}")
        try:
            db.finish_run(self.run_id, "failed", self.rows_loaded, self.checks, error[:2000],
                          dq_warnings=self.dq_warnings)
        except Exception as e:
            log.error(f"  Could not record the failure in the database ({type(e).__name__}).")


def setup_logging(stamp):
    """Log to the console and to logs/run_<stamp>.log."""
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOGS_DIR / f"run_{stamp}.log"
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logfile = logging.FileHandler(log_path, encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(console)
    log.addHandler(logfile)
    return log_path


def log_dq_warnings(warnings):
    log.info("Data-quality warnings (do not block the load):")
    if warnings is None:
        log.info("  (not calculated: the run stopped before cleaning finished)")
        return
    for name, w in warnings.items():
        flag = "WARN" if w["count"] else "ok  "
        log.info(f"  {flag}  {name}: {w['count']}" + (f"  ({w['detail']})" if w["count"] else ""))


def log_checks(checks, title):
    log.info(f"{title}:")
    for c in checks:
        log.info(f"  {'PASS' if c['passed'] else 'FAIL'}  {c['check']}  ({c['detail']})")


def write_parquet_copy(df, name, stamp):
    """Optional clean copy in data/stock/clean/, plus stock_latest.parquet."""
    settings.STOCK_CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow  # noqa: F401  (only checking it is installed)
        ext = "parquet"
        df.to_parquet(settings.STOCK_CLEAN_DIR / f"{name}_{stamp}.{ext}", index=False)
    except ImportError:
        log.warning("  pyarrow is not installed; writing CSV instead of parquet.")
        ext = "csv"
        df.to_csv(settings.STOCK_CLEAN_DIR / f"{name}_{stamp}.{ext}", index=False, encoding="utf-8-sig")
    shutil.copyfile(settings.STOCK_CLEAN_DIR / f"{name}_{stamp}.{ext}",
                    settings.STOCK_CLEAN_DIR / f"{LATEST_NAME}.{ext}")
    log.info(f"  Saved: {rel(settings.STOCK_CLEAN_DIR / f'{name}_{stamp}.{ext}')} (+ {LATEST_NAME}.{ext})")


# ── Clean -> validate -> load ────────────────────────────────────────────────
def process(raw_path, meta_path, name, stamp, run):
    """Clean, validate and load one raw file into erp.stock_snapshot.

    Returns True on success. A failed check marks the run 'failed' and returns
    False with nothing loaded; any other error is raised to main(), which records
    it against run.step. The load is one transaction, so a failed load leaves the
    day's previous rows intact.
    """
    run.at("cleaning")
    log.info(f"Cleaning {rel(raw_path)} (meta: {meta_path.name})")
    df, checks, totals = clean_export(raw_path, meta_path)
    try:
        run.dq_warnings = data_quality(df)
    except Exception as e:
        # Warnings are informational: a problem computing them must not stop the run
        log.warning(f"  Data-quality warnings not calculated ({type(e).__name__}: {e})")

    run.at("validation")
    run.checks = checks
    log_checks(checks, "Validation")
    failed = [c["check"] for c in checks if not c["passed"]]
    if failed:
        run.fail(f"Validation failed: {', '.join(failed)}. Nothing loaded.")
        return False

    run.at("loading")
    snapshot_date = df["snapshot_date"].iloc[0]
    run.rows_loaded = db.load_snapshot(df, snapshot_date, run.run_id)
    log.info(f"Loaded {run.rows_loaded} rows into erp.stock_snapshot for "
             f"{snapshot_date:%d/%m/%Y} (that day's previous rows replaced).")

    # Read back from the database and compare with the Excel totals row
    run.at("verification")
    stored = db.verify_snapshot(snapshot_date)
    log.info(f"verify_snapshot: {stored}")
    db_checks = [
        check("DB count == Total (N)", stored["count"] == totals["count"],
              f"{stored['count']} vs {totals['count']}"),
        check("DB sum gross_wt == totals row",
              abs(stored["sum_gross_wt"] - totals["gross_wt"]) <= SUM_TOLERANCE,
              f"{stored['sum_gross_wt']:,.3f} vs {totals['gross_wt']:,.3f}"),
        check("DB sum item_amt == totals row",
              abs(stored["sum_item_amt"] - totals["item_amt"]) <= SUM_TOLERANCE,
              f"{stored['sum_item_amt']:,.2f} vs {totals['item_amt']:,.2f}"),
    ]
    log_checks(db_checks, "Database check")
    run.checks = checks + db_checks
    db_failed = [c["check"] for c in db_checks if not c["passed"]]
    if db_failed:
        run.fail(f"Loaded, but database check failed: {', '.join(db_failed)}")
        return False

    # The parquet copy is optional: a problem here is a warning, not a failed run
    try:
        write_parquet_copy(df, name, stamp)
    except Exception as e:
        log.warning(f"  Parquet copy not written ({type(e).__name__}: {e})")
    run.succeed()
    return True


def check_only(raw_path, meta_path):
    """Clean + validate one raw file with no database and no ERP. Returns (ok, df)."""
    log.info(f"--check-only: cleaning {rel(raw_path)} (meta: {meta_path.name}); nothing is loaded.")
    df, checks, _ = clean_export(raw_path, meta_path)
    log_checks(checks, "Validation")
    log_dq_warnings(data_quality(df))
    return all(c["passed"] for c in checks), df


def files_for_raw(raw_path):
    """From <name>_<YYYYMMDD_HHMM>.xlsx work out name, stamp and the step A meta file."""
    m = re.match(r"(.+)_(\d{8}_\d{4})$", raw_path.stem)
    if not m:
        raise ErpError(f"Can't read <name>_<YYYYMMDD_HHMM> from '{raw_path.name}'.")
    name, stamp = m.groups()
    meta_path = settings.STOCK_META_DIR / f"{name}_{stamp}_stepA.json"
    if not meta_path.exists():
        raise ErpError(f"Matching step A file not found: {rel(meta_path)}")
    return name, stamp, meta_path


# ── CLI ──────────────────────────────────────────────────────────────────────
def int_list(text):
    """'1, 2,3' -> [1, 2, 3]; '' -> [] (meaning all)."""
    return [int(x) for x in text.split(",") if x.strip()]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Item-Wise Current Stock: ERP -> PostgreSQL.")
    ap.add_argument("--name", default=REPORT_NAME, help="report name used in output files")
    ap.add_argument("--run-endpoint", default=RUN_ENDPOINT, help="step A path after the host")
    ap.add_argument("--export-endpoint", default=EXPORT_ENDPOINT, help="step B path after the host")
    ap.add_argument("--locations", type=int_list, default=LOCATION_CODES,
                    help="comma-separated location codes; empty = all")
    ap.add_argument("--products", type=int_list, default=PRODUCT_CODES,
                    help="comma-separated product group codes")
    ap.add_argument("--items", type=int_list, default=ITEM_CODES,
                    help="comma-separated item codes; empty = all")
    ap.add_argument("--date", default=datetime.now().strftime("%d/%m/%Y"),
                    help="as-on date dd/mm/yyyy (default today)")
    ap.add_argument("--role", help="role code for setRoleCode (overrides ERP_ROLE_CODE)")
    ap.add_argument("--keep-logcode", action="store_true",
                    help="send stock_payload.json's logCode instead of the one from login")
    ap.add_argument("--from-file", type=Path,
                    help="clean + load an existing data/stock/raw/ Excel file; no ERP calls")
    ap.add_argument("--check-only", action="store_true",
                    help="with --from-file: clean + validate only; no database writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="build both request bodies and show the overrides; do not call the ERP")
    args = ap.parse_args(argv)
    try:
        datetime.strptime(args.date, "%d/%m/%Y")
    except ValueError:
        ap.error(f"--date must be dd/mm/yyyy, got '{args.date}'")
    if args.check_only and not args.from_file:
        ap.error("--check-only needs --from-file")
    return {"name": args.name, "run_endpoint": args.run_endpoint.strip("/"),
            "export_endpoint": args.export_endpoint.strip("/"),
            "locations": args.locations, "products": args.products,
            "items": args.items, "date": args.date, "dry_run": args.dry_run,
            "role": args.role, "keep_logcode": args.keep_logcode,
            "from_file": args.from_file, "check_only": args.check_only}


def dry_run(cfg):
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)
    filters = build_filters(cfg)
    a = step_a_body(filters)
    b = step_b_body(filters, "<reportID from step A>")
    print("Dry run, no ERP call.")
    print(f"Step A: POST {HOST}/{cfg['run_endpoint']}?sProgramKey={SPROGRAM_KEY}")
    for key in ("locationCodeList", "productCodeList", "itemCodeList", "labelDate"):
        print(f"  {key}: {a['params'][key]}")
    print(f"  step-B-only fields present: {[k for k in STEP_B_ONLY if k in a['params']]}")
    print("  logCode: (taken from login at run time)")
    print(f"Step B: POST {HOST}/{cfg['export_endpoint']}?sProgramKey={SPROGRAM_KEY}")
    print("  " + ", ".join(f"{k}: {b['params'][k]}" for k in STEP_B_ONLY))
    print(f"  printFormat: {b['printFormat']}, filterOn: {b['filterOn']}")


def main(argv=None):
    cfg = parse_args(argv)
    if cfg["dry_run"]:
        try:
            dry_run(cfg)
        except ErpError as e:
            sys.exit(f"ERROR: {e}")
        return

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M")

    if cfg["check_only"]:
        log.addHandler(logging.StreamHandler(sys.stdout))
        log.setLevel(logging.INFO)
        try:
            raw_path = cfg["from_file"].resolve()
            if not raw_path.exists():
                raise ErpError(f"File not found: {cfg['from_file']}")
            _, _, meta_path = files_for_raw(raw_path)
            ok, _ = check_only(raw_path, meta_path)
        except ErpError as e:
            sys.exit(f"ERROR: {e}")
        sys.exit(0 if ok else 1)

    log_path = setup_logging(run_stamp)

    # Record the run first, so every later failure is logged in erp.etl_run_log
    try:
        run = RunTracker("file" if cfg["from_file"] else "erp")
    except Exception as e:
        log.error(f"ERROR: could not start the run in the database: {type(e).__name__}: {e}")
        log.info(f"Log: {rel(log_path)}")
        sys.exit(1)

    ok = False
    try:
        if cfg["from_file"]:
            # Re-process an existing raw file: no login, no ERP calls
            raw_path = cfg["from_file"].resolve()
            if not raw_path.exists():
                raise ErpError(f"File not found: {cfg['from_file']}")
            name, stamp, meta_path = files_for_raw(raw_path)
            data = json.loads(meta_path.read_text(encoding="utf-8")).get("data") or {}
            run.record(report_id=data.get("reportID"), raw_file=str(rel(raw_path)),
                       record_count=(data.get("serverTotal") or {}).get("recordCount"))
            log.info("--from-file: no ERP calls.")
        else:
            run.at("config")
            erp_settings = settings.erp_login_settings()
            if cfg["role"]:
                erp_settings["role_code"] = cfg["role"]
            name, stamp = cfg["name"], run_stamp
            raw_path = settings.STOCK_RAW_DIR / f"{name}_{stamp}.xlsx"
            meta_path = settings.STOCK_META_DIR / f"{name}_{stamp}_stepA.json"
            fetch_report(cfg, erp_settings, raw_path, meta_path, run)

        ok = process(raw_path, meta_path, name, stamp, run)
    except (ErpError, settings.ConfigError) as e:
        run.fail(str(e))
    except Exception as e:
        run.fail(f"{type(e).__name__}: {e}")
    log_dq_warnings(run.dq_warnings)
    log.info(f"Log: {rel(log_path)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
