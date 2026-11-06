"""
Probe: per-label attributes and product images, for ~10 labels (one-off, read-only, gentle).

Per label, the same calls the ERP's Label Status screen makes:
  1. POST acme-inventory-web/inv/getLabelStatus           -> voucher rows, each with ownCode / itemCode / lablelNo
  2. GET  acme-inventory-web/inv/getInventoryAttributeForInv/{ownCode}/false -> Design No, Size, D Pattern, ...
  3. POST acme-document-web/doc/downloadBarcodeImage      -> the product image (exactly the screen's parameters)
Reports fill rates, image sizes and call times. Raw answers in data/probes/label_detail/ (git-ignored).
Nothing is written to the database.

Run:  .venv\\Scripts\\python scripts\\probes\\label_detail_probe.py [--pause 2]
"""
import argparse
import base64
import json
import time

import pandas as pd
from sqlalchemy import text

from kkj import settings
from kkj.erp import label_status
from kkj.erp.client import JSON_HEADERS, post, login, new_session, HOST, SPROGRAM_KEY

ap = argparse.ArgumentParser()
ap.add_argument("--pause", type=float, default=2)
args = ap.parse_args()
out = settings.PROJECT_DIR / "data" / "probes" / "label_detail"
out.mkdir(parents=True, exist_ok=True)

with settings.etl_engine().connect() as c:
    stock = pd.read_sql(text("""
        SELECT DISTINCT ON (item_name) barcode, item_name, set_item_barcode FROM erp.stock_snapshot
        WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot)
          AND item_name IN ('D RING', 'D TOPS', 'D PENDANT', 'D NECKLACE', 'LOOSE DIAMOND', 'D KUNDAN SET', 'D BANGLE', 'D BRACELET')
        ORDER BY item_name, label_dt DESC NULLS LAST"""), c)
    sold = pd.read_sql(text("""
        SELECT barcode, item_name, inventory_ref_code FROM sales.v_sales_line
        WHERE source = 'erp' AND pcs > 0 AND barcode IS NOT NULL ORDER BY sale_date DESC LIMIT 2"""), c)
sample = pd.concat([stock.assign(kind="in stock"), sold.assign(kind="sold")], ignore_index=True)
print(f"{len(sample)} labels")

erp = settings.erp_login_settings()
s = new_session(erp)
login(s, erp)
rows = []
first = True


def pause():
    global first
    if not first:
        time.sleep(args.pause)
    first = False


for r in sample.itertuples():
    rec = {"barcode": r.barcode, "item": r.item_name, "kind": r.kind}
    pause()
    t = time.time()
    hist = label_status.fetch(s, r.barcode, erp["role_code"])
    rec["status_s"] = round(time.time() - t, 2)
    (out / f"{r.barcode}_status.json").write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    owns = [h.get("ownCode") for h in hist if h.get("ownCode")]
    rec["vouchers"], rec["own_codes"] = len(hist), len(set(owns))
    if r.kind == "sold":
        rec["sales_ref_code"] = getattr(r, "inventory_ref_code", None)
        rec["ref_in_owncodes"] = str(rec["sales_ref_code"]) in {str(o) for o in owns}
    if not owns:
        rows.append(rec)
        continue
    own = owns[-1]                                          # the label's latest voucher
    pause()
    t = time.time()
    a = s.get(f"{HOST}/acme-inventory-web/inv/getInventoryAttributeForInv/{own}/false",
              params={"sProgramKey": SPROGRAM_KEY}, timeout=60)
    rec["attr_s"], rec["attr_http"] = round(time.time() - t, 2), a.status_code
    attrs = (a.json() or {}).get("data") if a.ok else None
    (out / f"{r.barcode}_attr.json").write_text(json.dumps(attrs, ensure_ascii=False), encoding="utf-8")
    if isinstance(attrs, list):
        for x in attrs:
            name = x.get("attributeName") or x.get("name") or x.get("attrName")
            val = x.get("attributeValue") or x.get("value") or x.get("attrValue") or x.get("valueName")
            if name:
                rec[f"attr:{name}"] = val
    pause()
    t = time.time()
    im = post(s, "acme-document-web/doc/downloadBarcodeImage", "image", headers=JSON_HEADERS,
              json={"params": {"fileId": r.barcode, "moduleCode": 46, "ftpMasterCode": 1, "storeOnFtp": 1,
                               "storeImgOn": 1, "uiCall": True}})
    rec["img_s"], rec["img_http"], rec["img_type"] = round(time.time() - t, 2), im.status_code, im.headers.get("Content-Type")
    body = im.content
    if "json" in (rec["img_type"] or ""):
        j = im.json()
        data = (j or {}).get("data")
        (out / f"{r.barcode}_img.json").write_text(json.dumps(j if not isinstance(data, str) else {**j, "data": f"<{len(data)} chars>"}),
                                                   encoding="utf-8")
        if isinstance(data, str) and len(data) > 100:
            try:
                body = base64.b64decode(data.split(",")[-1])
            except ValueError:
                body = b""
        else:
            body = b""
    rec["img_bytes"] = len(body)
    if body[:3] == b"\xff\xd8\xff":
        (out / f"{r.barcode}.jpg").write_bytes(body); rec["img_format"] = "jpeg"
    elif body[:8] == b"\x89PNG\r\n\x1a\n":
        (out / f"{r.barcode}.png").write_bytes(body); rec["img_format"] = "png"
    else:
        rec["img_format"] = "none" if not body else f"unknown {body[:8]!r}"
    rows.append(rec)
    print(f"  {r.barcode:12s} {r.item_name:14s} vouchers {rec['vouchers']:2d}  attr {rec.get('attr_http')}  "
          f"image {rec['img_format']} {rec['img_bytes']:,} B")

df = pd.DataFrame(rows)
df.to_csv(out / "summary.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print(df.to_string(index=False))
