"""
Weekly stock-as-on backfill for an earlier financial year (each year is its own ERP program).

  ERP_YEAR=2025-26 .venv\\Scripts\\python scripts\\backfill_year_stock.py --from 31/03/2025 --to 25/03/2026

Runs `python -m kkj.pipelines.stock --as-on --date d` for the first date and every 7 days after it, with the
year's login (ERP_YEAR). Each goes through the snapshot checks into stock_hist (source erp_asof); dates already
loaded are skipped; one failure doesn't stop the rest. Log: logs/backfill_<year>_<stamp>.log
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from kkj import settings  # noqa: E402
from overnight_backfill import run  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="start", required=True)
ap.add_argument("--to", dest="end", required=True)
ap.add_argument("--pause", type=float, default=5)
ap.add_argument("--legacy-dates", action="store_true", help="also every legacy-file snapshot date in the range")
ap.add_argument("--month-ends", action="store_true", help="the first date and each month's last day (not weekly)")
a = ap.parse_args()
year = os.environ.get("ERP_YEAR", "current")
start, end = (datetime.strptime(x, "%d/%m/%Y").date() for x in (a.start, a.end))
dates = [start]
if a.month_ends:                                                  # older years: month-end stock only
    d = (pd.Timestamp(start) + pd.offsets.MonthEnd(1)).date()
    while d <= end:
        dates.append(d)
        d = (pd.Timestamp(d) + pd.offsets.MonthEnd(1)).date()
else:
    d = start + timedelta(days=(2 - start.weekday()) % 7 or 7)    # then every Wednesday, like this year's
    while d <= end:
        dates.append(d)
        d += timedelta(days=7)
with settings.etl_engine().connect() as c:
    if a.legacy_dates:
        legacy = pd.read_sql(text("SELECT DISTINCT snapshot_date FROM stock_hist.snapshot_status WHERE source = 'csv_legacy'"), c)
        dates = sorted(set(dates) | {x for x in legacy["snapshot_date"] if start <= x <= end})
    have = set(pd.read_sql(text("SELECT snapshot_date FROM stock_hist.snapshot_status "
                                "WHERE source = 'erp_asof' AND status = 'loaded'"), c)["snapshot_date"])
todo = [x for x in dates if x not in have]
log_path = settings.LOGS_DIR / f"backfill_{year}_{datetime.now():%Y%m%d_%H%M}.log"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


log(f"{year}: {len(todo)} dates to load ({len(dates) - len(todo)} already there)")
ok = 0
for i, x in enumerate(todo):
    if i:
        time.sleep(a.pause)
    ok += bool(run(["--as-on", "--date", f"{x:%d/%m/%Y}"], log))
log(f"Done: {ok}/{len(todo)} loaded.")
