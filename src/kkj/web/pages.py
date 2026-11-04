"""Overview and Data checks pages."""
import plotly.graph_objects as go
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kkj import rules as rules_mod
from kkj.metrics.summary import headline
from kkj.viz import MUTED_SERIES, SERIES, style
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
        charts["series"] = style(fig, 280, legend=False).to_json()
        h["monthly_rows"] = m.assign(month=m["month"].dt.strftime("%b %Y")).to_dict("records")
        h["series_rows"] = s.assign(snapshot_date=s["snapshot_date"].dt.strftime("%d %b %Y")).to_dict("records")
    return render(request, "overview.html", "overview", h=h, charts=charts)


@router.get("/checks", response_class=HTMLResponse)
def checks(request: Request):
    return render(
        request, "checks.html", "checks",
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
                          ORDER BY snapshot_date DESC, source""").to_dict("records"))
