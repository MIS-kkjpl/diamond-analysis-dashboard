"""
Pull per-label details and product photos from the ERP into a local cache (data/labels/, git-ignored).
Read-only on the ERP; nothing goes into the database (a later step loads the cache).

Which labels, in this order:
  1. rings: every D RING in today's stock and every D RING sold since 01/05/2025 (the style pilot)
  2. the rest of today's stock, most valuable first
  3. (--sold-all) every other diamond piece sold since 01/05/2025

Per label (each skipped when already in the cache, so a stopped run just carries on next time):
  - getLabelStatus -> latest ownCode           -> data/labels/status/<barcode>.json
  - getInventoryAttributeForInv/<ownCode>       -> data/labels/attr/<barcode>.json
  - the full-resolution product photo, shrunk   -> data/labels/img/<barcode>.jpg (700 px) and
                                                   data/labels/thumb/<barcode>.jpg (200 px)
    (originals are never kept); if the full-res call has no photo, the ERP thumbnail
    (imgSrNo 0, the product shot) is kept instead. Loose stones: details only, no photo
    (they share one stock placeholder).
  - one line per label in data/labels/index.csv (what was found, timings)

No new label starts after --stop-at (default 23:30; the PC shuts down at midnight).

Run:  .venv\\Scripts\\python scripts\\pull_label_details.py [--stop-at 23:30] [--pause 0.5] [--workers 4] [--limit N] [--sold-all]
"""
import argparse
import base64
import csv
import io
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from PIL import Image
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kkj import settings  # noqa: E402
from kkj.erp import label_status  # noqa: E402
from kkj.erp.client import HOST, JSON_HEADERS, SPROGRAM_KEY, login, new_session, post  # noqa: E402

CACHE = settings.PROJECT_DIR / "data" / "labels"
DIRS = {k: CACHE / k for k in ("status", "attr", "img", "thumb")}
SOLD_FROM = "2025-05-01"
NO_PHOTO_ITEMS = ("LOOSE DIAMOND", "LOOSE DIAMOND (C)", "MRP DIAMOND")
FIELDS = ["barcode", "item", "why", "own_code", "attrs", "photo", "photo_from", "orig_px", "orig_bytes",
          "status_s", "attr_s", "img_s", "error", "at"]


def targets(sold_all):
    with settings.etl_engine().connect() as c:
        stock = pd.read_sql(text("""
            SELECT barcode, item_name, coalesce(taxable_amount, item_amt) AS value FROM erp.stock_snapshot
            WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot) AND barcode IS NOT NULL"""), c)
        sold = pd.read_sql(text("""
            SELECT barcode, max(item_name) AS item_name, max(sale_date) AS sold FROM sales.v_sales_line
            WHERE pcs > 0 AND barcode IS NOT NULL AND sale_date >= :d GROUP BY barcode"""), c, params={"d": SOLD_FROM})
    stock = stock.drop_duplicates("barcode").sort_values("value", ascending=False)
    sold = sold[~sold["barcode"].isin(stock["barcode"])]
    rings = pd.concat([stock[stock["item_name"] == "D RING"].assign(why="ring in stock"),
                       sold[sold["item_name"] == "D RING"].sort_values("sold", ascending=False).assign(why="ring sold")])
    rest = stock[stock["item_name"] != "D RING"].assign(why="in stock")
    parts = [rings, rest]
    if sold_all:
        parts.append(sold[sold["item_name"] != "D RING"].sort_values("sold", ascending=False).assign(why="sold"))
    return pd.concat(parts, ignore_index=True)[["barcode", "item_name", "why"]]


def b64(src):
    try:
        return base64.b64decode(str(src).split(",")[-1])
    except ValueError:
        return b""


def save_photo(raw, barcode):
    """700 px and 200 px JPEGs from the raw photo bytes; returns the original size."""
    with Image.open(io.BytesIO(raw)) as im:
        size = im.size
        im = im.convert("RGB")
        for name, px in (("img", 700), ("thumb", 200)):
            copy = im.copy()
            copy.thumbnail((px, px))
            copy.save(DIRS[name] / f"{barcode}.jpg", "JPEG", quality=85)
    return size


class Erp:
    """One ERP login shared by all workers (a second login could end the first one's session)."""
    def __init__(self):
        self.cfg = settings.erp_login_settings()
        self.s = new_session(self.cfg)
        self.lock = threading.Lock()
        self.logins = 0
        login(self.s, self.cfg)

    def call(self, fn):
        """Run fn(); on a rejected token log in again once (one worker only) and retry."""
        seen = self.logins
        r = fn()
        if getattr(r, "status_code", 200) in (401, 403):
            with self.lock:
                if self.logins == seen:              # nobody else has logged in again meanwhile
                    login(self.s, self.cfg)
                    self.logins += 1
            r = fn()
        return r


def pull_one(erp, r):
    """Details and photo for one label -> one index record. Never raises."""
    rec = {"barcode": r.barcode, "item": r.item_name, "why": r.why, "at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    try:
        t = time.time()
        hist = erp.call(lambda: label_status.fetch(erp.s, r.barcode, erp.cfg["role_code"]))
        rec["status_s"] = round(time.time() - t, 2)
        (DIRS["status"] / f"{r.barcode}.json").write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
        owns = [h.get("ownCode") for h in hist if h.get("ownCode")]
        if owns:
            rec["own_code"] = owns[-1]
            t = time.time()
            resp = erp.call(lambda: erp.s.get(
                f"{HOST}/acme-inventory-web/inv/getInventoryAttributeForInv/{owns[-1]}/false",
                params={"sProgramKey": SPROGRAM_KEY}, timeout=60))
            rec["attr_s"] = round(time.time() - t, 2)
            attrs = (resp.json() or {}).get("data") if resp.ok else None
            (DIRS["attr"] / f"{r.barcode}.json").write_text(json.dumps(attrs, ensure_ascii=False), encoding="utf-8")
            rec["attrs"] = len(attrs) if isinstance(attrs, list) else 0
        if r.item_name not in NO_PHOTO_ITEMS:
            t = time.time()
            resp = erp.call(lambda: post(
                erp.s, "acme-inventory-web/inv/getCriteriaStkSalesRptImages", "full image", timeout=180,
                headers=JSON_HEADERS,
                json={"params": {"reportParam": "1", "dataList": [{"index": 0, "barcode": r.barcode}],
                                 "storeImgOn": 1, "imageView": "1", "flagForOneStone": False,
                                 "stoneCodeList": [], "ftpMasterCode": 1}}))
            inner = (resp.json() or {}).get("data") if resp.ok else None
            items = inner.get("data") if isinstance(inner, dict) else inner
            raw = b64((items or [{}])[0].get("img") or (items or [{}])[0].get("src")) if items else b""
            rec["photo_from"] = "full"
            if not raw:
                resp = erp.call(lambda: post(
                    erp.s, "acme-document-web/doc/downloadBarcodeImage", "image", headers=JSON_HEADERS,
                    json={"params": {"fileId": r.barcode, "moduleCode": 46, "ftpMasterCode": 1,
                                     "storeOnFtp": 1, "storeImgOn": 1, "uiCall": True}}))
                data = (resp.json() or {}).get("data") if resp.ok else None
                shots = sorted(data or [], key=lambda x: x.get("imgSrNo", 9)) if isinstance(data, list) else []
                raw = b64(shots[0].get("src")) if shots else b""
                rec["photo_from"] = "thumbnail"
            rec["img_s"] = round(time.time() - t, 2)
            if raw:
                size = save_photo(raw, r.barcode)
                rec.update(photo=1, orig_px=f"{size[0]}x{size[1]}", orig_bytes=len(raw))
            else:
                rec.update(photo=0, photo_from="none")
    except Exception as e:                       # one label failing never stops the run
        rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-at", default="23:30")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds each worker waits between labels")
    ap.add_argument("--workers", type=int, default=1, help="labels pulled at the same time (one shared login)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sold-all", action="store_true")
    a = ap.parse_args()
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    index = CACHE / "index.csv"
    done = set(pd.read_csv(index)["barcode"]) if index.exists() else set()
    todo = targets(a.sold_all)
    todo = todo[~todo["barcode"].isin(done)]
    if a.limit:
        todo = todo.head(a.limit)
    print(f"{len(done)} labels already cached; {len(todo)} to pull with {a.workers} worker(s) "
          f"({todo['why'].value_counts().to_dict()})", flush=True)
    stop = datetime.strptime(a.stop_at, "%H:%M").time()
    erp = Erp()
    new_file = not index.exists()
    queue = iter(list(todo.itertuples()))
    take, write = threading.Lock(), threading.Lock()
    count = [0]

    def worker():
        while datetime.now().time() < stop:
            with take:
                r = next(queue, None)
            if r is None:
                return
            rec = pull_one(erp, r)
            with write:
                w.writerow(rec)
                f.flush()
                count[0] += 1
                if count[0] % 100 == 0:
                    print(f"{datetime.now():%H:%M} {count[0]}/{len(todo)} done", flush=True)
            time.sleep(a.pause)

    with open(index, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS)
        if new_file:
            w.writeheader()
        threads = [threading.Thread(target=worker) for _ in range(max(1, a.workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    left = len(todo) - count[0]
    print(f"Finished: {count[0]} labels this run" + (f"; stopped at {a.stop_at} with {left} left." if left else "."),
          flush=True)


if __name__ == "__main__":
    main()
