"""Read-only probe of the ERP sales report (1 day, Diamond Jewellery only).
Q1: does a client-made reportID work if we call the details endpoint first?
Q2: does the export endpoint then return an xlsx for that reportID?
Q3: does the details endpoint honour a large page size?
Tokens are never printed."""
import json, logging, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "archive" / "inventory_project"))
import config
import Phase_one as p1

OUT = Path(__file__).parent / "sales_probe"
OUT.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(message)s")
p1.log.addHandler(logging.StreamHandler(sys.stdout))

DATE = "04/11/2026"
ITEMS = [1908,2967,1913,1915,3187,1868,2465,2991,1921,1922,1872,2055,1925,1873,1927,2941,
         1874,1929,1931,2069,3072,2917,2851,2844,2999,2848,2849,2847,2845,2846,2715,3002]

settings = config.erp_login_settings()
s = p1.new_session(settings)
log_code = p1.login(s, settings)
rid = f"CriBasedSalesRpt{int(time.time() * 1000)}"
H = {"Content-Type": p1.JSON_CONTENT_TYPE}

def details(offset, limit):
    body = {"pg": {"offset": offset, "limit": limit}, "params": {
        "flagForLocation": True, "locationCodeList": [], "productCodeList": [14],
        "flagForPrev": False, "flagForNext": False, "reportID": rid, "searchString": "",
        "flagForSearch": False, "imageView": "2", "selectedStoneAttr": [], "selectedAttr": [],
        "selectdExtraCharges": [], "flagForItem": True, "itemCodeList": [], "flagForOneStone": False,
        "stoneCode": "", "stoneCodeList": [], "attrWiseRateVarience": True, "lblRptDiaRate": True,
        "ftpMasterCode": 1, "storeImgOn": 1, "storeOnFtp": 1, "supEntrySetting": 2,
        "logCode": log_code or 846635, "reportParam": "2", "exposeFTPToHTTP": False,
        "httpServerUrl": "", "noOfImages": 200, "sortedBy": [], "selectedImageType": "1",
        "selectCurPageImg": False, "selectAllPageImg": False, "amountRangeFilter": 0,
        "fromAmount": 0, "toAmount": 0, "piecesFilter": 0, "fromPieces": 0, "toPieces": 0,
        "labelDateFrom": "", "labelDateTo": "", "salesmanCodeList": [], "flagForSalesman": False,
        "onlySetItems": False, "subReportID": "", "allItemCodes": ITEMS, "viewSupplier": True,
        "fromDate": DATE, "toDate": DATE, "salesType": "1", "considerSalesReturn": "1"}}
    r = p1.post(s, "acme-inventory-web/inv/getCriteriaStkSalesRptDetails", "details",
                json=body, headers=H)
    print(f"details offset={offset} limit={limit}: HTTP {r.status_code}, {len(r.content)} B")
    try:
        return r.json()
    except ValueError:
        return {"_raw": r.text[:300]}

# Q1
d = details(0, 30)
(OUT / "details_p0.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
inner = d.get("data") if isinstance(d, dict) else None
rows = (inner or {}).get("data") or []
print("Q1 recTot:", (inner or {}).get("recTot"), "rows:", len(rows),
      "serverTotal:", (inner or {}).get("serverTotal"), "msg:", p1.server_messages(d))

# Q3
d2 = details(0, 5000)
rows2 = ((d2.get("data") or {}).get("data") or []) if isinstance(d2, dict) else []
print("Q3 rows with limit 5000:", len(rows2))

# Q2: export with same reportID
params = {"roleCode":648,"moduleCode":46,"applicationCode":1,"productFlag":1,"metalRateFlag":"2","imageView":"2",
 "locationCodeList":[],"productCodeList":[14],"supplierCodeList":[],"supplierNameList":[],"itemCodeList":[],
 "itemWtFrom":0,"itemWtTo":0,"itemAmtFrom":0,"itemAmtTo":0,"itemAttributes":[],"itemWtType":"1","itemCategory":[],
 "itemSubCategory":0,"stoneCode":[],"stoneName":"","stoneWtFrom":0,"stoneWtTo":0,"stoneAmtFrom":0,"stoneAmtTo":0,
 "stoneRateFrom":0,"stoneRateTo":0,"stoneAttributes":[],"stoneCodeList":[],"flagForOneStone":False,"isWtChecked":False,
 "isPcsChecked":False,"isAmtChecked":False,"isRateChecked":False,"selectedStoneAttr":[],"selectedAttr":[],
 "selectdExtraCharges":[],"flagForLocation":True,"flagForItem":True,"flagForSupplier":False,"showStockFor":1,
 "entryMode":1,"attrWiseRateVarience":True,"lblRptDiaRate":True,"ftpMasterCode":1,"storeImgOn":1,"storeOnFtp":1,
 "supEntrySetting":2,"logCode":log_code or 846635,"reportParam":"2","amountRangeFilter":0,"fromAmount":0,"toAmount":0,
 "piecesFilter":0,"fromPieces":0,"toPieces":0,"salesmanCodeList":[],"flagForSalesman":False,"showLabelsInItem":"3",
 "onlySetItems":False,"subReportID":"","allItemCodes":ITEMS,"exposeFTPToHTTP":False,"downloadImgByLink":"1",
 "httpServerUrl":"","orderFilterFlag":True,"showOrderItem":"2","viewSupplier":True,"fromDate":DATE,"toDate":DATE,
 "itemFromAge":0,"itemToAge":0,"flagForAge":False,
 "filterStr":[f" Date From: {DATE} To: {DATE} Location:All  Product Group:Diamond Jewellery      Show Labels In:Both  Show Order Labels:No ","Items:All         "],
 "isBarcodeCsvFileAttached":False,"salesType":"1","considerSalesReturn":"1","reportID":rid,"sortedBy":[],"exportReport":1}
r = p1.post(s, "acme-inventory-web/inv/getCriWiseSalesRpt", "export",
            json={"params": params, "printFormat": "excel", "filterOn": {"expLst": []}}, headers=H)
ct = r.headers.get("Content-Type", "")
print(f"Q2 export: HTTP {r.status_code}, {len(r.content)} B, {ct}, starts PK: {r.content[:2] == b'PK'}")
if r.content[:2] == b"PK":
    (OUT / "export.xlsx").write_bytes(r.content)
else:
    (OUT / "export_resp.txt").write_bytes(r.content[:2000])
    print("  body:", r.text[:300])
