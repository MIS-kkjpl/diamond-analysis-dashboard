"""
One-off overnight ERP backfill (run after the 2 am server backups, e.g. at 4 am):

  1. pg_dump kkj_erp_stock to C:\\Users\\kartik.riyal\\Software\\db_backups (safety copy)
  2. stock as on every Wednesday from 01/04/2026 to the last one before today
     -> stock_hist (source erp_asof), each through the snapshot checks
  3. today's stock -> erp.stock_snapshot (the production pipeline)
  4. refresh the decisions (python -m kkj.brain.run)

Each report runs as its own `python -m kkj.pipelines.stock ...` process, with a
pause between them; one failure is logged and the rest carry on. Summary in
logs/overnight_<stamp>.log.

Run:  .venv\\Scripts\\python scripts\\overnight_backfill.py [--plan] [--from 01/04/2026] [--pause 30]
      --plan prints what would run and exits (no ERP, no database).
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kkj import settings  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PG_DUMP = Path(r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe")
BACKUP_DIR = Path(r"C:\Users\kartik.riyal\Software\db_backups")


def weekly_dates(start, today):
    """Every 7 days from start up to (not including) today."""
    out, d = [], start
    while d < today:
        out.append(d)
        d += timedelta(days=7)
    return out


def backup(log):
    path = BACKUP_DIR / f"kkj_erp_stock_{datetime.now():%Y%m%d_%H%M}.dump"
    env = dict(os.environ, PGPASSWORD=settings.require("DIIS_DB_PASSWORD"))
    r = subprocess.run([str(PG_DUMP), "-h", settings.optional("DIIS_DB_HOST", "localhost"),
                        "-p", settings.optional("DIIS_DB_PORT", "5432"), "-U", settings.require("DIIS_DB_USER"),
                        "-d", "kkj_erp_stock", "-Fc", "-f", str(path)], env=env, capture_output=True, text=True)
    log(f"backup {path.name}: exit {r.returncode} {r.stderr.strip()[-200:]}")
    return r.returncode == 0


def run(args, log):
    t0 = time.time()
    r = subprocess.run([str(PYTHON), "-m", "kkj.pipelines.stock", *args], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    tail = [l for l in r.stdout.splitlines() if any(k in l for k in
            ("recordCount", "FAIL", "QUARANTINE", "Loaded", "read back", "ERROR", "Run "))]
    log(f"{' '.join(args)}: exit {r.returncode} in {time.time() - t0:.0f} s; " + " | ".join(tail[-4:]))
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="01/04/2026")
    ap.add_argument("--pause", type=float, default=30)
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    dates = weekly_dates(datetime.strptime(a.start, "%d/%m/%Y").date(), date.today())
    steps = [["--as-on", "--date", f"{d:%d/%m/%Y}"] for d in dates] + [[]]

    if a.plan:
        print(f"backup kkj_erp_stock, then {len(steps)} reports ({len(dates)} as-on + today's production):")
        for s in steps:
            print("  python -m kkj.pipelines.stock", " ".join(s) or "(today -> erp.stock_snapshot)")
        return

    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOGS_DIR / f"overnight_{datetime.now():%Y%m%d_%H%M}.log"

    def log(msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    if not backup(log):
        log("Backup failed: stopping before any load.")
        sys.exit(1)
    results = []
    for i, s in enumerate(steps):
        if i:
            time.sleep(a.pause)
        results.append(run(s, log))
    # 4. refresh the decisions on the new data
    r = subprocess.run([str(PYTHON), "-m", "kkj.brain.run", "--trigger", "pipeline"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    log(f"decisions refresh: exit {r.returncode}; {(r.stdout or r.stderr).strip().splitlines()[-1:]}")
    results.append(r.returncode == 0)
    log(f"Done: {sum(results)}/{len(results)} succeeded.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
