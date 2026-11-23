"""
The daily refresh (to be scheduled once the permanent setup is decided):

  1. pg_dump kkj_erp_stock (safety copy)
  2. sales for YESTERDAY and today: yesterday is always re-pulled in full, because a load during the
     day misses that evening's sales (4 Nov 2026: 20 of 29 lines)
  2b. the same days' order-booked sales (the report with "Show Order Labels: Only"), marked so the demand
      forecast leaves customer orders out (kkj.pipelines.order_booked)
  3. today's stock -> erp.stock_snapshot, and today's customer-order labels (which that pull leaves out) ->
     erp.order_snapshot
  3a. today's stock in Business Studio's 7 categories -> erp.stock_snapshot_bs (kkj.pipelines.stock_bs; runs side by
      side with erp.stock_snapshot until signed off)
  3b. the ERP voucher history of every label that left or arrived since the last snapshot, and of any label
      still unexplained (kkj.pipelines.label_vouchers -> erp.label_voucher), so the audit knows what happened
  4. refresh the recommendations, then the accuracy audit (python -m kkj.brain.run)
  5. if switched on (kkj.features): the AI second look; on Mondays the weekly brief

Every step runs as its own process and is logged; a failed step doesn't stop the later ones, except
that nothing runs without the backup. Log: logs/daily_<stamp>.log.

Run:  .venv\\Scripts\\python scripts\\daily_refresh.py
"""
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from kkj import settings  # noqa: E402
from overnight_backfill import PYTHON, backup  # noqa: E402


def step(name, args, log):
    t0 = datetime.now()
    r = subprocess.run([str(PYTHON), "-m", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    keep = [l for l in (r.stdout + r.stderr).splitlines() if any(k in l for k in (
        "recordCount", "FAIL", "Load ", "Loaded", "range(s)", "ERROR", "Error", "Run ", "Audit", "Fetched",
        "histories to fetch", "New on the lists"))]
    log(f"{name}: exit {r.returncode} in {(datetime.now() - t0).seconds} s; " + " | ".join(keep[-5:]))
    return r.returncode == 0


def main():
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOGS_DIR / f"daily_{datetime.now():%Y%m%d_%H%M}.log"

    def log(msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    if not backup(log):
        log("Backup failed: stopping before any load.")
        sys.exit(1)
    today = date.today()
    ok = [step("sales yesterday + today", ["kkj.pipelines.sales", "--from", f"{today - timedelta(days=1):%d/%m/%Y}",
                                           "--to", f"{today:%d/%m/%Y}"], log),
          step("order-booked sales yesterday + today", ["kkj.pipelines.sales", "--from",
                                                        f"{today - timedelta(days=1):%d/%m/%Y}", "--to",
                                                        f"{today:%d/%m/%Y}", "--order-labels", "3"], log),
          step("mark order-booked sales", ["kkj.pipelines.order_booked", "--pulled-on", f"{today:%Y%m%d}"], log),
          step("stock today", ["kkj.pipelines.stock"], log),
          step("customer-order labels today", ["kkj.pipelines.stock", "--order-labels"], log),
          step("stock today in Business Studio's categories", ["kkj.pipelines.stock_bs"], log),
          step("ERP voucher histories of the day's movements", ["kkj.pipelines.label_vouchers", "--recent", "1",
                                                                "--unexplained"], log),
          step("recommendations + audit", ["kkj.brain.run", "--trigger", "pipeline"], log)]
    # AI steps (kkj.features): only when switched on in .env with an API key; a failure here never fails the refresh
    from kkj import features
    if features.on("ai_review"):
        step("AI second look", ["kkj.ai.review"], log)
    if features.on("ai_brief") and today.weekday() == 0:
        step("weekly brief (Monday)", ["kkj.ai.brief"], log)
    log(f"Done: {sum(ok)}/{len(ok)} steps succeeded.")
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
