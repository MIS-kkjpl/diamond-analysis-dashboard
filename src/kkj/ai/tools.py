"""
Read-only lookups Ask Stockwise can call. Each runs a query through the dashboard account, returns compact JSON
(numbers rounded, at most ~40 rows) and never writes anything. Every figure in an answer has to come from one of
these, so the model cannot invent numbers.

TOOLS is the list sent to the API; run(name, input) executes one call.
"""
import json
import re

import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj import settings
from kkj.ai.client import share_names
from kkj.db import queries as q
from kkj.scope import location_kinds
from kkj.metrics import core as m
from kkj.metrics import sellers as sl
from kkj.metrics.summary import fy_start, headline

MAX_ROWS = 40


def _conn():
    return settings.app_engine().connect()


def _date(v, default):
    try:
        return pd.Timestamp(v).normalize() if v else default
    except (ValueError, TypeError):
        return default


def _table(df, cols=None, n=MAX_ROWS):
    """Rows as a list of dicts, numbers rounded; notes how many rows were left out."""
    df = df[cols] if cols else df
    out = []
    for r in df.head(n).to_dict("records"):
        row = {}
        for k, v in r.items():
            if isinstance(v, (float, np.floating)):
                v = None if not np.isfinite(v) else (round(float(v)) if abs(v) >= 100 else round(float(v), 3))
            elif isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, pd.Timestamp):
                v = v.date().isoformat()
            row[k] = v
        out.append(row)
    return {"rows": out, "rows_total": int(len(df)), "rows_shown": len(out)}


def _filter(df, **f):
    for col, val in f.items():
        if val:
            df = df[df[col].fillna("").astype(str).str.casefold().str.contains(str(val).casefold(), regex=False)]
    return df


def _short(b):
    return str(b).split(" / ")[0] if b else b


# ── the lookups ───────────────────────────────────────────────────────────────
def overview():
    with _conn() as c:
        h = headline(c)
    if not h:
        return {"error": "no stock data"}
    keep = ("as_of", "fy_start", "sales_to", "stock_value", "pcs", "labels", "net_sales_fy", "returns_share", "turn",
            "days_of_stock", "aged_value", "aged_share", "aged_days")
    return {k: (round(float(h[k]), 3) if isinstance(h[k], (float, np.floating)) else h[k]) for k in keep}


def stock(group_by="item_group", item_group=None, product=None, branch=None, older_than_days=None):
    with _conn() as c:
        s = q.stock_latest(c)
    s = s.assign(value=m.value(s), branch=s["branch"].map(_short), where=m.location_type(s["location"], location_kinds(c)),
                 age_band=m.age_bucket(s["age_days"]).astype(str))
    s = _filter(s, item_class_group=item_group, item_name=product, branch=branch)
    if older_than_days:
        s = s[s["age_days"] > float(older_than_days)]
    col = {"item_group": "item_class_group", "product": "item_name", "branch": "branch", "age_band": "age_band",
           "location_type": "where", "supplier": "supplier", "location": "location"}.get(group_by, "item_class_group")
    t = s.groupby(col, as_index=False).agg(labels=("barcode", "size"), pieces=("pcs", "sum"), value_rupees=("value", "sum"),
                                           median_age_days=("age_days", "median")).sort_values("value_rupees", ascending=False)
    return {"as_of": str(s["snapshot_date"].iloc[0])[:10] if len(s) else None, "total_value_rupees": round(float(s["value"].sum())),
            "total_labels": int(len(s)), **_table(t)}


def sales(start=None, end=None, group_by="month", item_group=None, product=None, branch=None):
    today = pd.Timestamp.today().normalize()
    start_d, end_d = _date(start, fy_start(today)), _date(end, today)
    with _conn() as c:
        lines = m.net_sales(q.sales_lines(c, start_d.date()))
        people = q.salespeople(c)
    lines = lines[(lines["sale_date"] >= start_d) & (lines["sale_date"] <= end_d)]
    lines = lines.assign(branch=lines["branch"].map(_short))
    lines = _filter(lines, item_class_group=item_group, item_name=product, branch=branch)
    if group_by == "salesperson":
        lines = sl.attribute(lines[lines["source"] != "csv_legacy"], people)
        lines = lines.assign(salesperson=lines["person"] if share_names() else lines["code"])
    col = {"month": "month", "item_group": "item_class_group", "product": "item_name", "branch": "branch",
           "salesperson": "salesperson", "day": "day"}.get(group_by, "month")
    lines = lines.assign(month=lines["sale_date"].dt.strftime("%Y-%m"), day=lines["sale_date"].dt.strftime("%Y-%m-%d"))
    t = lines.groupby(col, as_index=False).agg(net_sales_rupees=("net_value", "sum"), net_pieces=("net_pcs", "sum"),
                                               bills=("bill_no", "nunique"))
    t = t.sort_values(col if col in ("month", "day") else "net_sales_rupees", ascending=col in ("month", "day"))
    legacy = bool((lines["source"] == "csv_legacy").any()) if len(lines) else False
    return {"from": start_d.date().isoformat(), "to": end_d.date().isoformat(),
            "total_net_sales_rupees": round(float(lines["net_value"].sum())) if len(lines) else 0,
            "note": ("includes older sales from files: list value, no returns, no salesperson" if legacy else None),
            **_table(t, n=60 if col == "day" else MAX_ROWS)}


def stock_turn(search=None, verdict=None, branch=None, level="group"):
    scope = None
    with _conn() as c:
        if branch:
            scope = c.execute(text("SELECT DISTINCT scope FROM brain.brick_turn WHERE scope ILIKE :b LIMIT 1"),
                              {"b": f"%{branch}%"}).scalar()
        df = pd.read_sql(text("""SELECT brick, item_class_group, current_pcs, ideal_pcs, change_pcs, current_value,
                                        blended_turn, ideal_turn, ideal_source, status, sold_12m, as_of
                                 FROM brain.brick_turn WHERE as_of = (SELECT max(as_of) FROM brain.brick_turn)
                                   AND level = :lv AND scope = :sc"""),
                         c, params={"lv": "brick" if level == "brick" else "group", "sc": scope or "Company"})
    df = _filter(df, brick=search, status=verdict)
    df["size"] = df["change_pcs"].abs() * df["current_value"].div(df["current_pcs"].replace(0, np.nan)).fillna(0)
    df = df.sort_values("size", ascending=False).drop(columns="size")
    return {"scope": scope or "Company", "level": level,
            "explain": "turn = a year's sales / average stock held; ideal_turn = the turn the group's ideal stock implies "
                       "(no turn target is set by people); verdict compares the blended turn with it",
            **_table(df.drop(columns="as_of"))}


def recommendations(search=None, kind=None, status="open"):
    with _conn() as c:
        df = pd.read_sql(text("""SELECT action_id, kind, title, summary, branch, impact_value, impact_label, confidence,
                                        status, first_seen_at::date AS raised
                                 FROM brain.action WHERE status = ANY(:st) ORDER BY priority DESC"""),
                         c, params={"st": [status] if status in ("open", "approved", "snoozed", "rejected", "done", "resolved")
                                    else ["open"]})
    df = df.assign(branch=df["branch"].map(_short))
    if search:
        s = str(search).casefold()
        df = df[(df["title"].str.casefold().str.contains(s, regex=False)) | (df["summary"].fillna("").str.casefold().str.contains(s, regex=False))]
    df = _filter(df, kind=kind)
    return {"note": "kinds: transfer, understock (replenish), offer (markdown), liquidate (clearance), turn_increase/"
                    "turn_reduce/turn_phase_out (purchasing, from ideal stock), ideal_review (a fixed ideal due for review), festival, broken_set, supplier_slow/fast, discount",
            **_table(df, n=25)}


def piece(barcode):
    code = re.sub(r"[^A-Za-z0-9-]", "", str(barcode or "")).upper()[:20]
    with _conn() as c:
        now = pd.read_sql(text("""SELECT barcode, item_name, category, item_class_group, branch, location, age_days, dia_wt,
                                         gross_wt, coalesce(taxable_amount, item_amt) AS value_rupees, supplier, set_item_barcode
                                  FROM erp.stock_snapshot WHERE barcode = :b
                                    AND snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot)"""), c, params={"b": code})
        sold = pd.read_sql(text("""SELECT sale_date, bill_no, branch, item_name, pcs, coalesce(taxable_amount, item_amt) AS value_rupees
                                   FROM sales.v_sales_line WHERE barcode = :b ORDER BY sale_date"""), c, params={"b": code})
        recs = pd.read_sql(text("""SELECT action_id, title, status FROM brain.action
                                   WHERE pieces @> CAST(:p AS jsonb) ORDER BY action_id DESC LIMIT 10"""),
                           c, params={"p": json.dumps([{"barcode": code}])})
    if now.empty and sold.empty:
        return {"barcode": code, "found": False}
    return {"barcode": code, "in_stock_now": _table(now)["rows"], "sales": _table(sold)["rows"], "recommendations": _table(recs)["rows"]}


def seasonality(product=None, item_group=None):
    from kkj.metrics import seasonality as sn
    with _conn() as c:
        daily = q.hist_daily(c)
        fests = q.festivals(c)
        r = rules_mod.load(c)
    if daily.empty:
        return {"error": "no five-year history loaded"}
    key, val = ("item_name", product) if product else ("item_class_group", item_group or "Diamond Jewellery")
    sub = daily[daily[key].fillna("").str.casefold() == str(val).casefold()]
    idx = sn.monthly_index(sub, key, 1)
    lift = sn.festival_lift(sub, fests, key, r["festival_days_before"], r["festival_days_after"], 1)
    today = pd.Timestamp.today().normalize()
    ahead = fests[(fests["day"] >= today) & (fests["day"] <= today + pd.Timedelta(days=150))]
    return {"of": f"{key}={val}", "explain": "monthly index: 1.0 = an average month (April..March), from up to five years of pieces sold; "
                                             "festival lift: sales per day around the festival / that year's average day",
            "monthly_index": _table(idx)["rows"], "festival_lift": _table(lift)["rows"],
            "festivals_ahead": _table(ahead[["name", "day"]])["rows"]}


def salespeople(start=None, end=None, branch=None):
    today = pd.Timestamp.today().normalize()
    start_d, end_d = _date(start, fy_start(today)), _date(end, today)
    with _conn() as c:
        lines = m.net_sales(q.sales_lines(c, start_d.date()))
        people = q.salespeople(c)
        r = rules_mod.load(c)
    lines = lines[(lines["sale_date"] >= start_d) & (lines["sale_date"] <= end_d) & (lines["source"] != "csv_legacy")]
    card = sl.scorecard(sl.attribute(lines, people), r, r["aged_days"])
    if card.empty:
        return {"rows": [], "note": "no ERP sales with a salesperson in these dates"}
    card = card.assign(branch=card["branch"].map(_short), notes=card["notes"].map(lambda n: ", ".join(t for t, _ in n)))
    card = _filter(card, branch=branch)
    card = card[card["lines"] >= sl.MIN_LINES].sort_values("net_sales", ascending=False)
    who = "person" if share_names() else "code"
    return {"from": start_d.date().isoformat(), "to": end_d.date().isoformat(),
            "explain": "discount_vs_peers = discount given minus colleagues' at the same branch and price band (fraction); "
                       "vs_branch = sales per active month / the branch's typical regular seller",
            **_table(card[[who, "branch", "net_sales", "bills", "avg_bill", "discount", "discount_vs_peers", "return_rate",
                           "aged_share", "vs_branch", "notes"]])}


# ── definitions sent to the API ───────────────────────────────────────────────
def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
TOOLS = [
    {"name": "overview", "description": "Headline figures as of the latest stock snapshot: stock value, pieces, net sales "
     "this financial year, stock turn, aged stock. Start here for any company-level question.", "input_schema": _schema({})},
    {"name": "stock", "description": "Current stock grouped one way, with optional filters (partial names match). Values in "
     "rupees before GST.", "input_schema": _schema({
         "group_by": {"type": "string", "enum": ["item_group", "product", "branch", "age_band", "location_type", "supplier", "location"]},
         "item_group": S, "product": S, "branch": S, "older_than_days": {"type": "number"}}, ["group_by"])},
    {"name": "sales", "description": "Net sales (returns taken off) between two dates (YYYY-MM-DD; default: this financial "
     "year to today), grouped one way, with optional filters.", "input_schema": _schema({
         "start": S, "end": S, "group_by": {"type": "string", "enum": ["month", "day", "item_group", "product", "branch", "salesperson"]},
         "item_group": S, "product": S, "branch": S}, ["group_by"])},
    {"name": "stock_turn", "description": "Stock turn per brick group (purity · product · weight range) or brick: pieces now vs "
     "ideal, turn vs ideal turn, verdict (increase, reduce, keep, phase_out, unproven = held, no demand, too little time on display to stop it, ...). Search by part of the name, e.g. 'D RING'.",
     "input_schema": _schema({"search": S, "verdict": S, "branch": S,
                              "level": {"type": "string", "enum": ["group", "brick"]}})},
    {"name": "recommendations", "description": "Recommendations the system has raised, most important first, with their summary "
     "and money involved. Filter by words in the title or summary, kind, or status.",
     "input_schema": _schema({"search": S, "kind": S,
                              "status": {"type": "string", "enum": ["open", "approved", "snoozed", "rejected", "done", "resolved"]}})},
    {"name": "piece", "description": "One labelled piece by barcode (e.g. DRN21585): where it is, its sales, recommendations "
     "that include it.", "input_schema": _schema({"barcode": S}, ["barcode"])},
    {"name": "seasonality", "description": "When a product or item group sells through the year (monthly index) and around each "
     "festival (lift), plus festivals in the next 150 days. Give a product (e.g. 'D RING') or an item group.",
     "input_schema": _schema({"product": S, "item_group": S})},
    {"name": "salespeople", "description": "Salesperson scorecard (ERP sales only): net sales, bills, average bill, discount vs "
     "colleagues, returns, share of old stock sold, vs the branch's typical seller.",
     "input_schema": _schema({"start": S, "end": S, "branch": S})},
]
RUN = {"overview": overview, "stock": stock, "sales": sales, "stock_turn": stock_turn, "recommendations": recommendations,
       "piece": piece, "seasonality": seasonality, "salespeople": salespeople}


def run(name, args):
    """Execute one lookup; errors come back as data so the model can say what it couldn't find."""
    if name not in RUN:
        return {"error": f"unknown lookup {name}"}
    allowed = TOOLS[[t["name"] for t in TOOLS].index(name)]["input_schema"]["properties"]
    args = {k: v for k, v in (args or {}).items() if k in allowed and v not in (None, "")}
    try:
        return RUN[name](**args)
    except Exception as e:                       # a bad filter must not end the answer
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
