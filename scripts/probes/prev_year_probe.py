"""
Earlier financial year (default 2025-26, sProgramKey 54): does its ERP program give us
  1. stock as on past dates (the current program only goes back to 31/03/2026), and
  2. sales that agree with the legacy CSV for the same month?
Step A only (record counts and server totals). Read-only; no database writes.
Needs the year's login in .env: .venv\\Scripts\\python scripts\\set_erp_year.py 2025-26

Run:  .venv\\Scripts\\python scripts\\probes\\prev_year_probe.py [--year 2025-26]
"""
import argparse
import time

from sqlalchemy import text

from kkj import settings
from kkj.erp import sales_report, stock_report
from kkj.erp.client import JSON_HEADERS, item_codes_for_groups, login, new_session, post, server_messages
from kkj.pipelines.sales import PRODUCT_CODES as SALES_GROUPS

ap = argparse.ArgumentParser()
ap.add_argument("--year", default="2025-26")
ap.add_argument("--dates", default="")
ap.add_argument("--no-sales", action="store_true")
a = ap.parse_args()

erp = settings.erp_login_settings(a.year)
s = new_session(erp)
log_code = login(s, erp)
print(f"Logged in to the {a.year} program (sProgramKey {s.program_key})", flush=True)


def totals(r):
    body = r.json() if r.ok else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    t = data.get("serverTotal") or {}
    return t, ("" if t else f" server says: {server_messages(body)[:120]} (HTTP {r.status_code})")


print("\nStock as on (diamond groups, step A):")
for d in (a.dates.split(",") if a.dates else ["31/03/2026", "01/01/2026", "01/10/2025", "01/07/2025", "15/06/2025", "01/04/2025", "31/03/2025"]):
    p = stock_report.build_filters({"locations": stock_report.LOCATION_CODES, "products": stock_report.PRODUCT_CODES,
                                    "items": stock_report.ITEM_CODES, "date": d, "as_on": True}, log_code)
    t, why = totals(post(s, stock_report.RUN_ENDPOINT, "stock step A", json={"params": p}, headers=JSON_HEADERS,
                         timeout=300))
    print(f"  {d}: records {t.get('recordCount')}, pcs {t.get('pcs')}{why}", flush=True)
    time.sleep(10)

print("\nSales June 2025, ERP 2025-26 program vs legacy CSV:")
codes = item_codes_for_groups(s, SALES_GROUPS)
cfg = {"products": SALES_GROUPS, "from": "01/06/2025", "to": "30/06/2025", "sales_type": "1", "sales_return": "2"}
t, why = totals(post(s, sales_report.RUN_ENDPOINT, "sales step A", json={"params": sales_report.build_params(cfg, codes, log_code)},
                     headers=JSON_HEADERS, timeout=300))
with settings.etl_engine().connect() as c:
    leg = c.execute(text("""SELECT count(*), sum(pcs), sum(item_amt) FROM sales.v_sales_line
                            WHERE source = 'csv_legacy' AND sale_date BETWEEN '2025-06-01' AND '2025-06-30'""")).one()
print(f"  ERP:    records {t.get('recordCount')}, pcs {t.get('pcs')}, item_amt {t.get('itemamt')}{why}")
print(f"  legacy: lines {leg[0]}, pcs {leg[1]}, item_amt {leg[2]}")
