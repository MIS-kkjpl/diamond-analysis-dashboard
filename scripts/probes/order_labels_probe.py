"""
Does "Show Order Labels" hide sales of order-booked pieces? Step A only (record count and server totals)
for one range under each showOrderItem / orderFilterFlag setting.

Run:  .venv\\Scripts\\python scripts\\probes\\order_labels_probe.py 01/06/2026 30/06/2026
"""
import sys
import time

from kkj import settings
from kkj.erp.client import JSON_HEADERS, item_codes_for_groups, login, new_session, post, server_messages
from kkj.erp.sales_report import RUN_ENDPOINT, build_params
from kkj.erp.stock_report import PRODUCT_CODES

date_from, date_to = sys.argv[1], sys.argv[2]
erp = settings.erp_login_settings()
session = new_session(erp)
log_code = login(session, erp)
codes = item_codes_for_groups(session, PRODUCT_CODES)
cfg = {"products": PRODUCT_CODES, "from": date_from, "to": date_to, "sales_type": "1", "sales_return": "2"}
for show_order_item, flag in (("2", True), ("1", True), ("3", True), ("2", False)):
    params = build_params(cfg, codes, log_code)
    params.update(showOrderItem=show_order_item, orderFilterFlag=flag)
    r = post(session, RUN_ENDPOINT, "step A", json={"params": params}, headers=JSON_HEADERS)
    body = r.json() if r.ok else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    t = data.get("serverTotal") or {}
    print(f"showOrderItem {show_order_item}, orderFilterFlag {flag}: records {t.get('recordCount')}, pcs {t.get('pcs')}, "
          f"itemamt {t.get('itemamt')}" + ("" if t else f", server says: {server_messages(body)[:120]}"))
    time.sleep(5)
