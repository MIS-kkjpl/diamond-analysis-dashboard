"""
One label's full voucher history, as on the ERP's Label Status screen (showLabelStatus).

  POST acme-inventory-web/inv/getLabelStatus  {"params": {"barCode": ..., "setNo": 0, "setItemCode": 0,
                                                          "roleCode": <role>}}
  -> data: one row per voucher: docDate, docNo, voucherMaster (Labeling, Stock Transfer, BookedOut,
     BookedIn, ...), transactionType In/Out, pieces / piecesOut, locationName, orderNo, tag.

One small call per label (what a person opening the screen does). Used by the audit to find out
which transactions take labels out of stock.
"""
import pandas as pd

from kkj.erp.client import JSON_HEADERS, post

ENDPOINT = "acme-inventory-web/inv/getLabelStatus"


def fetch(session, barcode, role_code):
    r = post(session, ENDPOINT, f"label status {barcode}", headers=JSON_HEADERS,
             json={"params": {"barCode": barcode, "setNo": 0, "setItemCode": 0, "roleCode": int(role_code)}})
    r.raise_for_status()
    return (r.json() or {}).get("data") or []


def history(rows):
    """The voucher rows as a DataFrame in date order (ERP order kept within a day)."""
    cols = ["docDate", "docNo", "voucherMaster", "transactionType", "pieces", "piecesOut", "locationName",
            "orderNo", "tag", "cancelFlag"]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["docDate"] = pd.to_datetime(df["docDate"], format="%d/%m/%Y", errors="coerce")
    df["voucherMaster"] = df["voucherMaster"].astype(str).str.strip()
    return df.reset_index(names="seq").sort_values(["docDate", "seq"]).reset_index(drop=True)
