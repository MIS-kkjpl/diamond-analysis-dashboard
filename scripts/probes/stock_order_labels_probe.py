"""
Does the stock report's "Show Order Labels: No" hide pieces booked against customer orders? Step A only
(record count) for today's stock under each order-label setting. One report run per setting.

Run:  .venv\\Scripts\\python scripts\\probes\\stock_order_labels_probe.py
"""
import time
from datetime import date

from kkj import settings
from kkj.erp.client import JSON_HEADERS, login, new_session, post, server_messages
from kkj.erp.stock_report import ITEM_CODES, LOCATION_CODES, PRODUCT_CODES, RUN_ENDPOINT, build_filters

erp = settings.erp_login_settings()
session = new_session(erp)
log_code = login(session, erp)
cfg = {"locations": LOCATION_CODES, "products": PRODUCT_CODES, "items": ITEM_CODES, "date": f"{date.today():%d/%m/%Y}"}
for show_order, show_order_item in ((2, "2"), (2, "1"), (1, "1"), (2, "3")):
    filters = build_filters(cfg, log_code)
    filters.update(showOrder=show_order, showOrderItem=show_order_item)
    r = post(session, RUN_ENDPOINT, "step A", json={"params": filters}, headers=JSON_HEADERS)
    body = r.json() if r.ok else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    t = data.get("serverTotal") or {}
    print(f"showOrder {show_order}, showOrderItem {show_order_item}: records {t.get('recordCount')}, pcs {t.get('pcs')}"
          + ("" if t else f", server says: {server_messages(body)[:120]}"), flush=True)
    time.sleep(10)
