"""
Mark ERP sale lines of order-booked pieces (sales.order_booked_line) from order-only sales pulls.

The sales report run with "Show Order Labels: Only" (python -m kkj.pipelines.sales ... --order-labels 3) lists
exactly the sales of pieces booked against a customer order. Those are custom work, not demand the stock has to
meet, so the demand forecast leaves them out (sales.v_sales_line.order_booked). Each saved pull replaces the
marks inside its own date range, so re-pulling a period is safe.

  python -m kkj.pipelines.order_booked                      # every saved order-only pull (newest per range wins)
  python -m kkj.pipelines.order_booked data/sales/raw/<file>_o3_<stamp>.json.gz ...
  python -m kkj.pipelines.order_booked --pulled-on 20261110       # only the pulls made that day (daily refresh)
"""
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from kkj import settings
from kkj.erp.sales_report import clean, load_raw

RAW_DIR = settings.DATA_DIR / "sales" / "raw"
PATTERN = re.compile(r"sales_(\d{8})_(\d{8})_t\d+r\d+o3_(\d{8}_\d{6})\.json\.gz$")
log = logging.getLogger("kkj")


def latest_pulls(paths=None):
    """{(from, to): path}: the newest order-only pull for each date range."""
    best = {}
    for p in (paths or RAW_DIR.glob("*o3_*.json.gz")):
        m = PATTERN.search(Path(p).name)
        if m:
            rng = (datetime.strptime(m[1], "%Y%m%d").date(), datetime.strptime(m[2], "%Y%m%d").date())
            if rng not in best or m[3] > PATTERN.search(best[rng].name)[3]:
                best[rng] = Path(p)
    return best


def mark(conn, path, date_from, date_to):
    """Replace the marks for one pull's range with its lines; returns (lines, marks stored)."""
    df, checks, _ = clean(load_raw(path))
    if not all(c["passed"] for c in checks):
        raise ValueError(f"{path.name}: the pull failed its checks; not used.")
    keys = df.dropna(subset=["bill_no", "inventory_ref_code"]).drop_duplicates(["bill_no", "inventory_ref_code"])
    conn.execute(text("DELETE FROM sales.order_booked_line WHERE report_from >= :a AND report_to <= :b"),
                 {"a": date_from, "b": date_to})
    if len(keys):
        conn.execute(text("""
            INSERT INTO sales.order_booked_line (bill_no, inventory_ref_code, sale_date, report_from, report_to)
            VALUES (:bill, :ref, :d, :a, :b)
            ON CONFLICT (bill_no, inventory_ref_code) DO UPDATE
            SET sale_date = EXCLUDED.sale_date, report_from = EXCLUDED.report_from, report_to = EXCLUDED.report_to,
                marked_at = now()"""),
            [{"bill": r.bill_no, "ref": int(r.inventory_ref_code), "d": r.sale_date, "a": date_from, "b": date_to}
             for r in keys.itertuples()])
    return len(df), len(keys)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    day = None
    if "--pulled-on" in args:
        i = args.index("--pulled-on")
        day, args = args[i + 1], args[:i] + args[i + 2:]
    pulls = latest_pulls([Path(a) for a in args] if args else None)
    if day:
        pulls = {r: p for r, p in pulls.items() if PATTERN.search(p.name)[3].startswith(day)}
    if not pulls:
        log.info("No order-only pulls found (run the sales pipeline with --order-labels 3).")
        return
    with settings.etl_engine().begin() as conn:
        total = 0
        for (a, b), path in sorted(pulls.items()):
            lines, stored = mark(conn, path, a, b)
            total += stored
            log.info(f"{a} to {b}: {lines} order-booked lines, {stored} marked ({path.name})")
        matched = conn.execute(text("""SELECT count(*) FILTER (WHERE order_booked), count(*)
                                       FROM sales.v_sales_line WHERE source = 'erp'""")).one()
    log.info(f"{total} marks from {len(pulls)} pulls; {matched[0]:,} of {matched[1]:,} ERP sale lines are order-booked.")


if __name__ == "__main__":
    main()
