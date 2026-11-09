"""
How far back does the ERP's "stock as on a past date" go? Step A only (record count) for a few dates.
6 Nov 2026: 29/05/2025 returned 0 records. Read-only; no database writes.

Run:  .venv\\Scripts\\python scripts\\probes\\asof_range_probe.py 01/04/2025 01/10/2025 ...
"""
import sys
import time

from kkj import settings
from kkj.erp.client import JSON_HEADERS, login, new_session, post, server_messages
from kkj.erp.stock_report import ITEM_CODES, LOCATION_CODES, PRODUCT_CODES, RUN_ENDPOINT, build_filters

erp = settings.erp_login_settings()
session = new_session(erp)
log_code = login(session, erp)
for d in sys.argv[1:]:
    filters = build_filters({"locations": LOCATION_CODES, "products": PRODUCT_CODES, "items": ITEM_CODES, "date": d},
                            log_code)
    filters.update(labelDate=d, isLabelAsOnDate=1, calStkAsOnLabelDate=True, flagForItem=False)
    r = post(session, RUN_ENDPOINT, "step A", json={"params": filters}, headers=JSON_HEADERS, timeout=300)
    body = r.json() if r.ok else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    t = data.get("serverTotal") or {}
    print(f"{d}: records {t.get('recordCount')}, pcs {t.get('pcs')}"
          + ("" if t else f", server says: {server_messages(body)[:120]}"), flush=True)
    time.sleep(10)
