"""Lookup: one search box (top bar) for a barcode, a product, a salesperson, a brick group or a recommendation."""
import json

import numpy as np
import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kkj.db import queries as q
from kkj.metrics import core as m
from kkj.web import charts
from kkj.web.common import cached, read, render

router = APIRouter()
MONTH_NAMES = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


def _piece(barcode):
    """Everything about one label: where it is now, where it has been, its sales, recommendations naming it."""
    now = read("""SELECT snapshot_date, branch, location, item_class_group, item_name, category, purity, pcs, gross_wt,
                         dia_wt, coalesce(taxable_amount, item_amt) AS value, age_days, supplier,
                         coalesce(label_dt, labeling_date) AS label_date, order_no
                  FROM erp.stock_snapshot WHERE upper(barcode) = :b
                    AND snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot)""", b=barcode)
    seen = read("""SELECT snapshot_date, branch, location, source FROM (
                     SELECT snapshot_date, branch, location, 'ERP daily' AS source FROM erp.stock_snapshot WHERE upper(barcode) = :b
                     UNION ALL
                     SELECT snapshot_date, branch, location, CASE source WHEN 'erp_asof' THEN 'ERP as-on' ELSE 'Legacy file' END
                     FROM stock_hist.snapshot WHERE upper(barcode) = :b) s ORDER BY snapshot_date""", b=barcode)
    sold = read("""SELECT sale_date, bill_no, branch, salesman, pcs, item_amt, taxable_amount, source
                   FROM sales.v_sales_line WHERE upper(barcode) = :b ORDER BY sale_date""", b=barcode)
    recs = read("""SELECT action_id, title, status, kind FROM brain.action
                   WHERE pieces @> CAST(:p AS jsonb) ORDER BY last_seen_at DESC""", p=json.dumps([{"barcode": barcode}]))
    if now.empty and seen.empty and sold.empty:
        return None
    # moves: one row per change of branch/location
    moves = []
    for r in seen.itertuples():
        here = (r.branch, r.location)
        if not moves or moves[-1]["where"] != here:
            moves.append({"from": r.snapshot_date, "to": r.snapshot_date, "where": here, "source": r.source})
        else:
            moves[-1]["to"] = r.snapshot_date
    return {"barcode": barcode, "now": now.iloc[0].to_dict() if len(now) else None,
            "first_seen": seen["snapshot_date"].min() if len(seen) else None, "moves": moves,
            "sold": sold.to_dict("records"), "recs": recs.to_dict("records")}


def _product(item):
    stock = m.company_stock(cached(q.stock_latest))
    stock = stock[stock["item_name"].str.casefold() == item.casefold()]
    if stock.empty:
        return None
    stock = stock.assign(value=m.value(stock))
    by_branch = stock.groupby("branch").agg(pieces=("pcs", "sum"), value=("value", "sum"), median_age=("age_days", "median"),
                                            over_365_days=("age_days", lambda a: float((a > 365).mean()))).reset_index()
    by_branch["branch"] = by_branch["branch"].str.split(" / ").str[0]
    sales = m.net_sales(cached(q.sales_lines, (pd.Timestamp.today() - pd.DateOffset(months=12)).date()))
    sales = sales[sales["item_name"].fillna("").str.casefold() == item.casefold()]
    monthly = sales.assign(month=sales["sale_date"].dt.to_period("M")).groupby("month") \
        .agg(pieces=("net_pcs", "sum"), value=("net_value", "sum")).reset_index()
    monthly["month"] = monthly["month"].dt.strftime("%b %Y")
    groups = read("""SELECT brick AS brick_group, current_pcs, ideal_pcs, blended_turn, ideal_turn, status
                     FROM brain.brick_turn WHERE scope = 'Company' AND level = 'group' AND lower(item_name) = lower(:i)
                       AND as_of = (SELECT max(as_of) FROM brain.brick_turn) ORDER BY current_pcs DESC""", i=item)
    from kkj.metrics import seasonality as sn
    daily = cached(q.hist_daily)
    idx = sn.monthly_index(daily[daily["item_name"].fillna("").str.casefold() == item.casefold()], "item_name", 50) \
        if len(daily) else pd.DataFrame()
    recs = read("""SELECT action_id, title, status, kind FROM brain.action WHERE status IN ('open', 'approved', 'snoozed')
                   AND (title ILIKE :p OR subject->>'item' = :i) ORDER BY priority DESC LIMIT 20""", p=f"% {item}%", i=item)
    return {"item": stock["item_name"].iloc[0], "group": stock["item_class_group"].iloc[0],
            "pieces": float(stock["pcs"].sum()), "value": float(stock["value"].sum()),
            "t_branch": charts.table(by_branch, {"pieces": "num", "value": "money", "median_age": "num", "over_365_days": "pct"},
                                     {"median_age": "median age (days)", "over_365_days": "over 365 days"}),
            "t_month": charts.table(monthly, {"pieces": "num", "value": "money"}) if len(monthly) else None,
            "sold_12m": float(sales["net_pcs"].sum()) if len(sales) else 0.0,
            "t_groups": charts.table(groups, {"current_pcs": "num", "ideal_pcs": "num", "blended_turn": "dec", "ideal_turn": "dec"},
                                     {"current_pcs": "now (pcs)", "ideal_pcs": "ideal (pcs)", "blended_turn": "turn",
                                      "ideal_turn": "ideal turn", "status": "verdict"}) if len(groups) else None,
            "fig_season": charts.season_lines(MONTH_NAMES, {"Monthly pattern": [idx.iloc[0][f"index_{mm}"] for mm in sn.MONTHS]})
            if len(idx) else None,
            "recs": recs.to_dict("records")}


@router.get("/lookup", response_class=HTMLResponse)
def lookup(request: Request):
    text_q = request.query_params.get("q", "").strip()[:80]
    ctx = {"query": text_q, "piece": None, "product": None, "products": [], "people": [], "groups": [], "recs": []}
    if len(text_q) >= 2:
        like = f"%{text_q}%"
        ctx["piece"] = _piece(text_q.upper()) if " " not in text_q else None
        names = read("""SELECT item_name, count(*) AS labels FROM erp.stock_snapshot
                        WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot) AND item_name ILIKE :p
                        GROUP BY 1 ORDER BY 2 DESC LIMIT 15""", p=like)
        ctx["products"] = names.to_dict("records")
        exact = names[names["item_name"].str.casefold() == text_q.casefold()]
        if len(exact) or len(names) == 1:
            ctx["product"] = _product((exact if len(exact) else names).iloc[0]["item_name"])
        ctx["people"] = read("""SELECT sales_code, name, branch, active FROM config.salesperson
                                WHERE name ILIKE :p OR sales_code ILIKE :p OR employee_code ILIKE :p
                                ORDER BY active DESC, name LIMIT 15""", p=like).to_dict("records")
        ctx["groups"] = read("""SELECT brick, level, status, current_pcs FROM brain.brick_turn
                                WHERE scope = 'Company' AND brick ILIKE :p AND as_of = (SELECT max(as_of) FROM brain.brick_turn)
                                ORDER BY level DESC, current_pcs DESC LIMIT 15""", p=like).to_dict("records")
        ctx["recs"] = read("""SELECT action_id, title, status, kind FROM brain.action
                              WHERE status IN ('open', 'approved', 'snoozed') AND (title ILIKE :p OR summary ILIKE :p)
                              ORDER BY priority DESC LIMIT 15""", p=like).to_dict("records")
    ctx["nothing"] = len(text_q) >= 2 and not any(ctx[k] for k in ("piece", "product", "products", "people", "groups", "recs"))
    return render(request, "lookup.html", "lookup", **ctx)
