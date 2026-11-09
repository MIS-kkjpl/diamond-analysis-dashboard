"""
Probe: does the ERP still return product photos for labels that have been SOLD, and how far back?
(Needed before any image tagging: turn-by-style needs sold pieces tagged, not just current stock.)

Read-only, gentle. Per sampled sold label:
  - POST acme-document-web/doc/downloadBarcodeImage  (thumbnails, every photo of the label)
For a few labels also the full-resolution call design-studio uses:
  - POST acme-inventory-web/inv/getCriteriaStkSalesRptImages
Full-res originals are NOT kept: each is shrunk to 700 px JPEG (what tagging would use). Nothing goes to the database.

Run:  .venv\\Scripts\\python scripts\\probes\\sold_image_probe.py [--per-bucket 8] [--full 6] [--pause 2]
Output: data/probes/sold_images/ (git-ignored): summary.csv, thumbnails, 700 px JPEGs.
"""
import argparse
import base64
import hashlib
import io
import json
import time

import pandas as pd
from PIL import Image
from sqlalchemy import text

from kkj import settings
from kkj.erp.client import JSON_HEADERS, post, login, new_session

ap = argparse.ArgumentParser()
ap.add_argument("--per-bucket", type=int, default=8)
ap.add_argument("--full", type=int, default=6)
ap.add_argument("--pause", type=float, default=2)
args = ap.parse_args()
out = settings.PROJECT_DIR / "data" / "probes" / "sold_images"
out.mkdir(parents=True, exist_ok=True)

BUCKETS = [("0-1 m", 0, 30), ("1-3 m", 30, 91), ("3-6 m", 91, 182), ("6-12 m", 182, 365), ("12-18 m", 365, 550)]
with settings.etl_engine().connect() as c:
    parts = []
    for name, lo, hi in BUCKETS:
        parts.append(pd.read_sql(text("""
            SELECT barcode, item_name, max(sale_date) AS sale_date, min(source) AS source FROM sales.v_sales_line
            WHERE pcs > 0 AND barcode IS NOT NULL
              AND sale_date >  current_date - make_interval(days => :hi)
              AND sale_date <= current_date - make_interval(days => :lo)
              AND barcode NOT IN (SELECT barcode FROM erp.stock_snapshot
                                  WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot))
            GROUP BY barcode, item_name ORDER BY random() LIMIT :n"""), c,
            params={"lo": lo, "hi": hi, "n": args.per_bucket}).assign(bucket=name))
sample = pd.concat(parts, ignore_index=True)
print(f"{len(sample)} sold labels (none of them in today's stock)")

erp = settings.erp_login_settings()
s = new_session(erp)
login(s, erp)
first = True


def pause():
    global first
    if not first:
        time.sleep(args.pause)
    first = False


def decode(src):
    try:
        return base64.b64decode(str(src).split(",")[-1])
    except ValueError:
        return b""


def dims(b):
    try:
        with Image.open(io.BytesIO(b)) as im:
            return im.size
    except Exception:
        return None


rows = []
for r in sample.itertuples():
    rec = {"barcode": r.barcode, "item": r.item_name, "sold": r.sale_date, "bucket": r.bucket, "source": r.source}
    pause()
    t = time.time()
    resp = post(s, "acme-document-web/doc/downloadBarcodeImage", "image", headers=JSON_HEADERS,
                json={"params": {"fileId": r.barcode, "moduleCode": 46, "ftpMasterCode": 1, "storeOnFtp": 1,
                                 "storeImgOn": 1, "uiCall": True}})
    rec["thumb_s"], rec["http"] = round(time.time() - t, 2), resp.status_code
    data = (resp.json() or {}).get("data") if resp.ok and "json" in resp.headers.get("Content-Type", "") else None
    imgs = data if isinstance(data, list) else []
    rec["photos"] = len(imgs)
    rec["sr_nos"] = ",".join(str(i.get("imgSrNo")) for i in imgs)
    for i in imgs:
        b = decode(i.get("src"))
        if not b:
            continue
        sr = i.get("imgSrNo")
        rec[f"hash{sr}"] = hashlib.md5(b).hexdigest()[:10]
        rec[f"size{sr}"] = dims(b)
        when = (i.get("fileIdOnMongo") or {}).get("timestamp")
        if when:
            rec[f"uploaded{sr}"] = pd.Timestamp(when, unit="s").date()
        (out / f"{r.barcode}_{sr}_thumb.png").write_bytes(b)
    rows.append(rec)
    print(f"  {r.bucket:7s} {r.barcode:12s} {r.item_name:16s} sold {r.sale_date}  photos {rec['photos']}  {rec['thumb_s']} s")

# Full resolution for a few labels that had a photo, spread over the buckets
df = pd.DataFrame(rows)
with_photo = df[df["photos"] > 0]
pick = with_photo.groupby("bucket", sort=False).head(2).head(args.full)
for idx, r in pick.iterrows():
    pause()
    t = time.time()
    resp = post(s, "acme-inventory-web/inv/getCriteriaStkSalesRptImages", "full image", timeout=180, headers=JSON_HEADERS,
                json={"params": {"reportParam": "1", "dataList": [{"index": 0, "barcode": r.barcode}], "storeImgOn": 1,
                                 "imageView": "1", "flagForOneStone": False, "stoneCodeList": [], "ftpMasterCode": 1}})
    df.loc[idx, "full_s"], df.loc[idx, "full_http"] = round(time.time() - t, 2), resp.status_code
    try:
        j = resp.json()
    except ValueError:
        j = None
    inner = (j or {}).get("data")
    items = inner.get("data") if isinstance(inner, dict) else inner
    items = items if isinstance(items, list) else []
    df.loc[idx, "full_photos"] = len(items)
    if not items:
        (out / f"{r.barcode}_full_raw.json").write_text(json.dumps(j)[:4000] if j else resp.text[:4000], encoding="utf-8")
    for k, it in enumerate(items):
        b = decode(it.get("img") or it.get("src"))
        if not b:
            continue
        if k == 0:
            df.loc[idx, "full_bytes"] = len(b)
            df.loc[idx, "full_size"] = str(dims(b))
        try:
            with Image.open(io.BytesIO(b)) as im:
                im = im.convert("RGB")
                im.thumbnail((700, 700))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
            (out / f"{r.barcode}_{k}_700.jpg").write_bytes(buf.getvalue())
            if k == 0:
                df.loc[idx, "jpg700_bytes"] = len(buf.getvalue())
        except Exception as e:
            df.loc[idx, "full_err"] = type(e).__name__
    print(f"  full-res {r.barcode:12s} {df.loc[idx, 'full_s']} s  photos {len(items)}  "
          f"{df.loc[idx].get('full_size')}  {int(df.loc[idx].get('full_bytes') or 0):,} B")

df.to_csv(out / "summary.csv", index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
print()
print(df.groupby("bucket", sort=False).agg(labels=("barcode", "size"), with_photo=("photos", lambda x: (x > 0).sum()),
                                           two_photos=("photos", lambda x: (x > 1).sum())).to_string())
