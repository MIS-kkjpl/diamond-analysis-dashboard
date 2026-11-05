"""Excel exports people act on: transfer slips, markdown lists, the purchasing list, one recommendation's pieces."""
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
            rows.append({"recommendation": a["action_id"], "status": a["status"], "recommendation title": a["title"],
                         **{k: v for k, v in subject.items()}, **p})
    return pd.DataFrame(rows)


@router.get("/transfers.xlsx")
def transfers(status: str = "both"):
    df = _pieces("transfer", STATUS.get(status, STATUS["both"]))
    if df.empty:
        return Response("No transfer recommendations to export.", status_code=404)
    df["from"] = df["from"].str.split(" / ").str[0]
    df["to"] = df["to"].str.split(" / ").str[0]
    cols = ["recommendation", "status", "from", "to", "group", "barcode", "item_name", "location", "age_days", "dia_wt", "value"]
    df = df[[c for c in cols if c in df]]
    return _xlsx({"All transfers": df, **{f"From {b}": g for b, g in df.groupby("from")}}, "transfer_slips")


@router.get("/offers.xlsx")
def offers(status: str = "both"):
    df = _pieces("offer", STATUS.get(status, STATUS["both"]))
    if df.empty:
        return Response("No markdown recommendations to export.", status_code=404)
    df["branch"] = df["recommendation title"].str.extract(r" at (\S+)$")[0]
    cols = ["recommendation", "status", "branch", "item", "barcode", "group", "location", "age_days", "value", "offer_pct"]
    df = df[[c for c in cols if c in df]].rename(columns={"offer_pct": "suggested markdown %"})
    return _xlsx({"All markdowns": df, **{b: g for b, g in df.groupby("branch")}}, "markdown_lists")


@router.get("/buying.xlsx")
def buying(status: str = "both"):
    s = STATUS.get(status, STATUS["both"])
    q = """SELECT action_id AS recommendation, status, kind, title, summary, impact_label, confidence
           FROM brain.action WHERE kind = ANY(:k) AND status = ANY(:s) ORDER BY priority DESC"""
    groups = read("""SELECT brick AS brick_group, item_class_group, current_pcs AS now_pcs, ideal_pcs, change_pcs,
                             round(change_pcs * avg_piece_value) AS change_value, blended_turn, ideal_turn, ideal_source,
                             sold_12m, status AS verdict
                      FROM brain.brick_turn WHERE scope = 'Company' AND level = 'group'
                        AND as_of = (SELECT max(as_of) FROM brain.brick_turn)
                      ORDER BY abs(change_pcs * coalesce(avg_piece_value, 0)) DESC""")
    return _xlsx({"Increase": read(q, k=["turn_increase"], s=s),
                  "Reduce": read(q, k=["turn_reduce"], s=s),
                  "Phase out": read(q, k=["turn_phase_out"], s=s),
                  "All brick groups": groups,
                  "Replenishment": read(q, k=["understock"], s=s),
                  "Suppliers": read(q, k=["supplier_slow", "supplier_fast"], s=s)}, "purchasing_list")


@router.get("/recommendation/{action_id}.xlsx")
def recommendation_pieces(action_id: int):
    a = read("SELECT title, pieces FROM brain.action WHERE action_id = :a", a=action_id)
    if a.empty:
        return Response("No such recommendation", status_code=404)
    pieces = a["pieces"].iloc[0]
    pieces = pieces if isinstance(pieces, list) else json.loads(pieces or "[]")
    return _xlsx({"Pieces": pd.DataFrame(pieces)}, f"recommendation_{action_id}")
