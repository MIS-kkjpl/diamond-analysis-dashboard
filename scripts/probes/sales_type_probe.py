"""
Which ERP sales types exist? Step A only (record count and server totals, no detail pages) for each
salesType over one range. The screen offers: All Sales, Retail Sales, Franchisee Sales, Rd Sales.

Run:  .venv\\Scripts\\python scripts\\probes\\sales_type_probe.py 15/07/2026 15/08/2026
"""
import sys
import time

from kkj import settings
from kkj.erp.client import JSON_HEADERS, item_codes_for_groups, login, new_session, post, server_messages
from kkj.erp.sales_report import RUN_ENDPOINT, build_params
from kkj.erp.stock_report import PRODUCT_CODES

date_from, date_to = (sys.argv[1:3] + [None, None])[:2]
date_to = date_to or date_from
erp = settings.erp_login_settings()
session = new_session(erp)
log_code = login(session, erp)
codes = item_codes_for_groups(session, PRODUCT_CODES)
for sales_type in ("0", "1", "2", "3", "4", "5"):
    cfg = {"products": PRODUCT_CODES, "from": date_from, "to": date_to, "sales_type": sales_type, "sales_return": "2"}
    r = post(session, RUN_ENDPOINT, "step A", json={"params": build_params(cfg, codes, log_code)}, headers=JSON_HEADERS)
    body = r.json() if r.ok else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    t = data.get("serverTotal") or {}
    print(f"salesType {sales_type}: HTTP {r.status_code}, records {t.get('recordCount')}, pcs {t.get('pcs')}, "
          f"itemamt {t.get('itemamt')}" + ("" if t else f", server says: {server_messages(body)[:120]}"))
    time.sleep(5)
