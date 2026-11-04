"""Excel exports people act on: transfer slips, offer lists, the buying list, one decision's pieces."""
import io
import json
from datetime import date

import pandas as pd
from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse

from kkj.web.common import read

router = APIRouter(prefix="/exports")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUS = {"open": ["open"], "approved": ["approved"], "both": ["open", "approved"]}


def _xlsx(sheets, name):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for sheet, df in sheets.items():
            df.to_excel(xl, sheet_name=str(sheet)[:31], index=False)
            ws = xl.sheets[str(sheet)[:31]]
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(45, max(10, *(len(str(c.value or "")) for c in col)))
    buf.seek(0)
    return StreamingResponse(buf, media_type=XLSX,
                             headers={"Content-Disposition": f'attachment; filename="{name}_{date.today():%Y%m%d}.xlsx"'})


def _pieces(kind, statuses):
    rows = []
    for a in read("""SELECT action_id, status, title, subject, pieces FROM brain.action
                     WHERE kind = :k AND status = ANY(:s) ORDER BY priority DESC""", k=kind, s=statuses).to_dict("records"):
        subject = a["subject"] if isinstance(a["subject"], dict) else json.loads(a["subject"] or "{}")
        pieces = a["pieces"] if isinstance(a["pieces"], list) else json.loads(a["pieces"] or "[]")
        for p in pieces:
            rows.append({"decision": a["action_id"], "status": a["status"], "decision title": a["title"],
                         **{k: v for k, v in subject.items()}, **p})
    return pd.DataFrame(rows)


@router.get("/transfers.xlsx")
def transfers(status: str = "both"):
    df = _pieces("transfer", STATUS.get(status, STATUS["both"]))
    if df.empty:
        return Response("No transfer decisions to export.", status_code=404)
    df["from"] = df["from"].str.split(" / ").str[0]
    df["to"] = df["to"].str.split(" / ").str[0]
    cols = ["decision", "status", "from", "to", "group", "barcode", "item_name", "location", "age_days", "dia_wt", "value"]
    df = df[[c for c in cols if c in df]]
    return _xlsx({"All transfers": df, **{f"From {b}": g for b, g in df.groupby("from")}}, "transfer_slips")


@router.get("/offers.xlsx")
def offers(status: str = "both"):
    df = _pieces("offer", STATUS.get(status, STATUS["both"]))
    if df.empty:
        return Response("No offer decisions to export.", status_code=404)
    df["branch"] = df["decision title"].str.extract(r" at (\S+) on offer")[0]
    cols = ["decision", "status", "branch", "item", "barcode", "group", "location", "age_days", "value", "offer_pct"]
    df = df[[c for c in cols if c in df]].rename(columns={"offer_pct": "suggested offer %"})
    return _xlsx({"All offers": df, **{b: g for b, g in df.groupby("branch")}}, "offer_lists")


@router.get("/buying.xlsx")
def buying(status: str = "both"):
    s = STATUS.get(status, STATUS["both"])
    q = """SELECT action_id AS decision, status, kind, title, summary, impact_label, confidence
           FROM brain.action WHERE kind = ANY(:k) AND status = ANY(:s) ORDER BY priority DESC"""
    return _xlsx({"Buy (running short)": read(q, k=["understock"], s=s),
                  "Pause buying": read(q, k=["pause_buying"], s=s),
                  "Price bands": read(q, k=["buy_band"], s=s),
                  "Suppliers": read(q, k=["supplier_slow", "supplier_fast"], s=s)}, "buying_list")


@router.get("/decision/{action_id}.xlsx")
def decision_pieces(action_id: int):
    a = read("SELECT title, pieces FROM brain.action WHERE action_id = :a", a=action_id)
    if a.empty:
        return Response("No such decision", status_code=404)
    pieces = a["pieces"].iloc[0]
    pieces = pieces if isinstance(pieces, list) else json.loads(pieces or "[]")
    return _xlsx({"Pieces": pd.DataFrame(pieces)}, f"decision_{action_id}")
