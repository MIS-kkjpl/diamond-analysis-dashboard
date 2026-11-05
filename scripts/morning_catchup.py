"""
One-off morning catch-up after the 5 Nov 2026 audit (runs when the PC is on, quiet morning hours):

  1. backup
  2. re-pull ERP sales 01/04/2026 -> today, month by month, now INCLUDING sales of order-booked pieces
  3. stock report order-label test (record counts only)
  4. today's stock
  5. ERP as-on stock on every legacy-file date without an as-on snapshot, and in every fortnight with no
     snapshot (current financial year first; then one 2025-26 date as a test, and the rest only if it loads)
  6. recommendations + audit; clear the data notice if every sales month re-pulled cleanly
  7. a short summary in logs/morning_catchup_<stamp>.md

No new ERP report starts after --stop-at (default 13:30); what was skipped is listed.

Run:  .venv\\Scripts\\python scripts\\morning_catchup.py [--stop-at 13:30] [--pause 30] [--plan]
"""
import argparse
import glob
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from kkj import notice, settings  # noqa: E402
from overnight_backfill import PYTHON, backup, run as stock_run  # noqa: E402
from daily_refresh import step  # noqa: E402

FY_START = date(2026, 4, 1)


def as_on_dates():
    """Legacy-file dates with no ERP as-on snapshot, plus a date inside every fortnight that has none."""
    with settings.etl_engine().connect() as c:
        st = pd.read_sql(text("SELECT snapshot_date, source, status FROM stock_hist.snapshot_status"), c)
    have = set(st.loc[(st["source"] == "erp_asof") & (st["status"] == "loaded"), "snapshot_date"])
    dates = set(st.loc[st["source"] == "csv_legacy", "snapshot_date"]) - have
    audits = sorted(glob.glob(str(ROOT / "data" / "audit" / "audit_*.xlsx")))
    if audits:
        gaps = pd.read_excel(audits[-1], "L1 stock fortnights missing")
        dates |= {(pd.Timestamp(e) - pd.Timedelta(days=7)).date() for e in gaps["fortnight_ending"]}
    dates = sorted(d for d in dates if d < date.today())
    return [d for d in dates if d >= FY_START], [d for d in dates if d < FY_START]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-at", default="13:30")
    ap.add_argument("--pause", type=float, default=30)
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    this_fy, last_fy = as_on_dates()
    today = date.today()
    if a.plan:
        print(f"sales 01/04/2026..{today:%d/%m/%Y} by month (with order labels); stock order-label test; today's stock")
        print(f"as-on, this FY ({len(this_fy)}): {[f'{d:%d/%m/%Y}' for d in this_fy]}")
        print(f"as-on, 2025-26 ({len(last_fy)}; first one is a test): {[f'{d:%d/%m/%Y}' for d in last_fy]}")
        return

    stop_at = datetime.combine(today, datetime.strptime(a.stop_at, "%H:%M").time())
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    log_path = settings.LOGS_DIR / f"morning_catchup_{stamp}.log"
    summary = []

    def log(msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def time_left(what):
        if datetime.now() >= stop_at:
            log(f"Stopped at {a.stop_at}; not run: {what}")
            summary.append(f"- Not run (after {a.stop_at}): {what}")
            return False
        return True

    if not backup(log):
        log("Backup failed: stopping before any load.")
        sys.exit(1)

    sales_ok = time_left("sales re-pull") and step(
        "sales re-pull with order labels", ["kkj.pipelines.sales", "--from", "01/04/2026", "--to", f"{today:%d/%m/%Y}",
                                            "--by-month", "--pause", "20"], log)
    summary.append(f"- Sales 01/04/2026–{today:%d/%m/%Y} re-pulled with order-booked pieces: {'OK' if sales_ok else 'FAILED'}")

    if time_left("stock order-label test"):
        r = subprocess.run([str(PYTHON), str(ROOT / "scripts" / "probes" / "stock_order_labels_probe.py")], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        lines = [l for l in r.stdout.splitlines() if l.startswith("showOrder")]
        log("stock order-label test: " + " | ".join(lines))
        summary.append("- Stock order-label test: " + "; ".join(lines))

    if time_left("today's stock"):
        ok = stock_run([], log)
        summary.append(f"- Today's stock: {'OK' if ok else 'FAILED'}")

    loaded, failed, skipped = [], [], []
    queue = [(d, "this FY") for d in this_fy] + [(d, "2025-26") for d in last_fy]
    tested_last_fy = None
    for d, fy in queue:
        if fy == "2025-26" and tested_last_fy is False:
            skipped.append(d)
            continue
        if not time_left(f"as-on {d:%d/%m/%Y} and later"):
            skipped += [x for x, _ in queue[queue.index((d, fy)):]]
            break
        time.sleep(a.pause)
        ok = stock_run(["--as-on", "--date", f"{d:%d/%m/%Y}"], log)
        (loaded if ok else failed).append(d)
        if fy == "2025-26" and tested_last_fy is None:
            tested_last_fy = ok
            log(f"2025-26 test date {d}: {'loaded, continuing' if ok else 'did not load, skipping the other 2025-26 dates'}")
    summary.append(f"- As-on stock: {len(loaded)} loaded, {len(failed)} failed or set aside "
                   f"({', '.join(map(str, failed)) or '–'}), {len(skipped)} not run")

    ok = step("recommendations + audit", ["kkj.brain.run", "--trigger", "pipeline"], log)
    summary.append(f"- Recommendations and audit: {'OK' if ok else 'FAILED'}")
    if sales_ok:
        with settings.app_engine().begin() as c:
            notice.clear(c, "morning catch-up")
        summary.append("- Data notice cleared (sales now include order-booked pieces)")

    md = settings.LOGS_DIR / f"morning_catchup_{stamp}.md"
    md.write_text(f"# Morning catch-up {datetime.now():%d %b %Y %H:%M}\n\n" + "\n".join(summary) +
                  f"\n\nFull log: {log_path.name}. Audit: Stockwise → Data quality.\n", encoding="utf-8")
    log(f"Summary: {md}")


if __name__ == "__main__":
    main()
