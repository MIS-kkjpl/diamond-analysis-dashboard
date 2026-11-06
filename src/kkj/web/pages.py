"""Overview and Data checks pages."""
import json

import plotly.graph_objects as go
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from kkj import rules as rules_mod
from kkj.metrics.summary import headline
from kkj.viz import MUTED_SERIES, SERIES, style
from kkj import settings
from kkj.db import queries as q
from kkj.web import charts
from kkj.web.common import engine, read, render

router = APIRouter()


@router.get("/overview", response_class=HTMLResponse)
def overview(request: Request):
    with engine().connect() as conn:
        rules = rules_mod.load(conn)
        h = headline(conn, rules["aged_days"])
    charts = {}
    if h:
        m = h["monthly"]
        fig = go.Figure()
        for source, colour, name in (("csv_legacy", MUTED_SERIES, "Legacy file (label value, no returns)"),
                                     ("erp", SERIES[0], "ERP (taxable, net of returns)")):
            part = m[m["source"] == source]
            if len(part):
                fig.add_bar(x=part["month"], y=part["net"] / 1e7, name=name, marker_color=colour,
                            customdata=part["pcs"],
                            hovertemplate="%{x|%b %Y}<br>₹%{y:.2f} Cr · %{customdata:,.0f} pcs<extra></extra>")
        fig.update_yaxes(tickprefix="₹", ticksuffix=" Cr")
        fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
        charts["monthly"] = style(fig, 300).to_json()
        s = h["series"]
        fig = go.Figure(go.Scatter(x=s["snapshot_date"], y=s["value"] / 1e7, mode="lines+markers",
                                   line=dict(color=SERIES[0], width=2),
                                   marker=dict(size=8, color=SERIES[0], line=dict(color="#ffffff", width=2)),
                                   customdata=s[["source", "pcs"]],
                                   hovertemplate="%{x|%d %b %Y}<br>₹%{y:.2f} Cr<br>%{customdata[1]:,.0f} pcs · "
                                                 "%{customdata[0]}<extra></extra>"))
        fig.update_yaxes(tickprefix="₹", ticksuffix=" Cr")
        charts["series"] = style(fig, 300, legend=False).to_json()
        h["monthly_rows"] = m.assign(month=m["month"].dt.strftime("%b %Y")).to_dict("records")
        h["series_rows"] = s.assign(snapshot_date=s["snapshot_date"].dt.strftime("%d %b %Y")).to_dict("records")
    return render(request, "overview.html", "overview", h=h, charts=charts)


def _latest_audit():
    a = read("""SELECT audit_id, at AT TIME ZONE 'Asia/Kolkata' AS at, passed, failed, info, results, exceptions, report_file
                FROM brain.audit_run ORDER BY audit_id DESC LIMIT 1""")
    if a.empty:
        return None
    a = a.iloc[0].to_dict()
    for k in ("results", "exceptions"):
        if isinstance(a[k], str):
            a[k] = json.loads(a[k])
    a["issues"] = sorted((r for r in a["results"] if r["result"] != "PASS"), key=lambda r: (r["result"] != "FAIL", r["layer"]))
    a["layers"] = {}
    for r in a["results"]:
        a["layers"].setdefault(r["layer"], {"PASS": 0, "FAIL": 0, "INFO": 0})[r["result"]] += 1
    return a


@router.get("/checks/audit.xlsx")
def audit_workbook():
    """The latest audit's exception lists."""
    a = read("SELECT report_file FROM brain.audit_run ORDER BY audit_id DESC LIMIT 1")
    if a.empty:
        return Response("No audit yet", status_code=404)
    path = (settings.PROJECT_DIR / a["report_file"].iloc[0]).resolve()
    audit_dir = (settings.PROJECT_DIR / "data" / "audit").resolve()
    if audit_dir not in path.parents or not path.exists():
        return Response("Report not found", status_code=404)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/checks", response_class=HTMLResponse)
def checks(request: Request):
    return render(
        request, "checks.html", "checks", audit=_latest_audit(),
        stock_runs=read("""SELECT run_id, started_at AT TIME ZONE 'Asia/Kolkata' AS started, source, status,
                                  record_count, rows_loaded, error FROM erp.etl_run_log ORDER BY run_id DESC LIMIT 20"""
                        ).to_dict("records"),
        sales_loads=read("""SELECT load_id, started_at AT TIME ZONE 'Asia/Kolkata' AS started, source, date_from,
                                   date_to, status, rows_loaded, error FROM sales.load_log ORDER BY load_id DESC LIMIT 20"""
                         ).to_dict("records"),
        brain_runs=read("""SELECT run_id, started_at AT TIME ZONE 'Asia/Kolkata' AS started, trigger, status,
                                  candidates, created, updated, resolved, reopened, error
                           FROM brain.run_log ORDER BY run_id DESC LIMIT 20""").to_dict("records"),
        snapshots=read("""SELECT snapshot_date, source, status, reason, labels FROM stock_hist.snapshot_status
                          ORDER BY snapshot_date DESC, source""").to_dict("records"),
        t_bills=_odd_bills(), big_discount=q.BIG_DISCOUNT)


def _odd_bills():
    """Sale lines worth a second look this financial year (kkj.db.queries.odd_bills)."""
    import pandas as pd
    from kkj.metrics.summary import fy_start
    from kkj.salespeople import split_salesman
    with engine().connect() as conn:
        df = q.odd_bills(conn, fy_start(pd.Timestamp.today().normalize()).date())
    if df.empty:
        return None
    df = df.assign(branch=df["branch"].map(lambda b: str(b).split(" / ")[0] if b else "–"),
                   salesman=df["salesman"].map(lambda v: split_salesman(v)[1] if isinstance(v, str) and v.strip() else "–"),
                   barcode=[(b, f"/lookup?q={b}") if isinstance(b, str) and b else ("–", "") for b in df["barcode"]])
    return charts.table(df[["issue", "sale_date", "bill_no", "branch", "salesman", "barcode", "item_name", "label_value",
                            "billed", "discount"]],
                        {"sale_date": "date", "barcode": "link", "label_value": "money", "billed": "money", "discount": "pct"},
                        {"sale_date": "date", "salesman": "salesperson", "item_name": "item"})
