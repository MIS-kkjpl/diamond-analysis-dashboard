"""Full chain, read-only: step A (getRptKeyAndServerTotalForSalesRpt) -> details (big page) -> export.
Usage: python sales_probe3.py dd/mm/yyyy dd/mm/yyyy [all]"""
import io, json, logging, re, sys, time
from pathlib import Path
import pandas as pd, requests
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "archive" / "inventory_project"))
import config, Phase_one as p1
logging.basicConfig(level=logging.INFO, format="%(message)s")
FROM, TO = sys.argv[1], sys.argv[2]
ALL = len(sys.argv) > 3
PRODUCTS = [5, 100, 126, 14, 121, 127, 69, 18] if ALL else [14]
HERE = Path(__file__).resolve().parent
OUT = HERE / "sales_probe"; OUT.mkdir(exist_ok=True)
H = {"Content-Type": p1.JSON_CONTENT_TYPE}
settings = config.erp_login_settings(); s = p1.new_session(settings); log_code = p1.login(s, settings) or 846635

# item codes live, per product group
items = []
for pc in PRODUCTS:
    r = p1.post(s, "acme-document-web/doc/getItemCodeListOfPrdGrp", "items", json={"params": {"proSubChildList": [pc], "onlySetItems": False}}, headers=H)
    items += (r.json().get("data") or {}).get("itemCodeList") or []
items = sorted(set(items)); print("products", PRODUCTS, "-> item codes:", len(items))

src = (HERE / "sales_probe.py").read_text()
ns = {}; exec(re.search(r"(params = \{.*?\"exportReport\":1\})", src, re.S).group(1), {"log_code": log_code, "ITEMS": items, "DATE": FROM, "rid": "X", "False": False, "True": True}, ns)
P = ns["params"]
P.update(productCodeList=PRODUCTS, allItemCodes=items, fromDate=FROM, toDate=TO, logCode=log_code,
         filterStr=[f" Date From: {FROM} To: {TO} Location:All  Product Group:Diamond      Show Labels In:Both  Show Order Labels:No ", "Items:All         "])
A = {k: v for k, v in P.items() if k not in ("reportID", "sortedBy", "exportReport")}

t0 = time.time()
r = p1.post(s, "acme-inventory-web/inv/getRptKeyAndServerTotalForSalesRpt", "step A", json={"params": A}, headers=H, timeout=600)
d = r.json(); inner = d.get("data") or {}
rid = inner.get("reportID"); tot = inner.get("serverTotal") or {}
print(f"step A {time.time()-t0:.1f}s HTTP {r.status_code} reportID={rid} recordCount={tot.get('recordCount')} pcs={tot.get('pcs')} itemamt={tot.get('itemamt')} msg={p1.server_messages(d) if not rid else ''}")
Path(OUT / f"stepA_{FROM.replace('/','')}_{TO.replace('/','')}.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
if not rid: sys.exit()

det = {k: P[k] for k in ("flagForLocation","locationCodeList","productCodeList","imageView","selectedStoneAttr","selectedAttr","selectdExtraCharges","flagForItem","itemCodeList","flagForOneStone","attrWiseRateVarience","lblRptDiaRate","ftpMasterCode","storeImgOn","storeOnFtp","supEntrySetting","logCode","reportParam","amountRangeFilter","fromAmount","toAmount","piecesFilter","fromPieces","toPieces","salesmanCodeList","flagForSalesman","onlySetItems","subReportID","allItemCodes","viewSupplier","fromDate","toDate","salesType","considerSalesReturn")}
det.update(reportID=rid, flagForPrev=False, flagForNext=False, searchString="", flagForSearch=False, stoneCode="", stoneCodeList=[], exposeFTPToHTTP=False, httpServerUrl="", noOfImages=0, sortedBy=[], selectedImageType="1", selectCurPageImg=False, selectAllPageImg=False, labelDateFrom="", labelDateTo="")
t0 = time.time(); rows = []; off = 0; LIMIT = 2000
while True:
    r = p1.post(s, "acme-inventory-web/inv/getCriteriaStkSalesRptDetails", "details", json={"pg": {"offset": off, "limit": LIMIT}, "params": det}, headers=H, timeout=600)
    page = ((r.json().get("data") or {}).get("data")) or []
    rows += page; off += LIMIT
    if len(page) < LIMIT: break
print(f"details {time.time()-t0:.1f}s rows={len(rows)} (pages of {LIMIT})")
df = pd.DataFrame(rows); df.to_parquet(OUT / f"details_{FROM.replace('/','')}_{TO.replace('/','')}.parquet")
if len(df):
    print("check pcs", df.pcs.sum(), "vs", tot.get("pcs"), "| itemamt", round(df.itemamt.sum(),2), "vs", tot.get("itemamt"))
    print("columns:", len(df.columns)); print("groups:", df.groupname.value_counts().to_dict())
    print("returns flag:", df.salesReturnDoneFlag.value_counts().to_dict(), "| neg pcs:", (df.pcs < 0).sum(), "| neg amt:", (df.itemamt < 0).sum())
    print("taxable/itemamt ratio:", round(df.taxableamount.sum()/df.itemamt.sum(), 4), "| dup barcodes:", df.barcode.duplicated().sum(), "| dup itemInvOwnCode:", df.itemInvOwnCode.duplicated().sum())
    print("bill prefixes:", df.billno.str.extract(r"^[A-Z]+\d*([A-Z]+)-")[0].value_counts().to_dict())

t0 = time.time()
r = p1.post(s, "acme-inventory-web/inv/getCriWiseSalesRpt", "export", json={"params": P | {"reportID": rid}, "printFormat": "excel", "filterOn": {"expLst": []}}, headers=H, timeout=600)
ok = r.content[:2] == b"PK"
print(f"export {time.time()-t0:.1f}s HTTP {r.status_code} xlsx={ok} {len(r.content)//1024} kB")
if ok:
    (OUT / f"export_{FROM.replace('/','')}_{TO.replace('/','')}.xlsx").write_bytes(r.content)
else:
    print(p1.server_messages(r.json()) if "json" in r.headers.get("Content-Type","") else r.text[:200])
