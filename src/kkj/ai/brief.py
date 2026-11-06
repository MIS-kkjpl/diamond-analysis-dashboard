"""
The weekly management brief: the week's figures are computed here (facts()), then Claude writes them up as one
page for management. The facts are stored with the brief, so every statement can be checked.

Run:  python -m kkj.ai.brief            # write this week's brief now (also: the Weekly brief page)
"""
import argparse
import json

import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj import notice, settings
from kkj.ai import client as ai
from kkj.brain import outcomes
from kkj.db import queries as q
from kkj.metrics import core as m
from kkj.metrics.summary import headline

KIND_LABELS = {"transfer": "Transfer", "understock": "Replenish", "offer": "Markdown", "liquidate": "Clearance",
               "supplier_slow": "Supplier (slow)", "supplier_fast": "Supplier (fast)", "turn_increase": "Purchasing: increase",
               "turn_reduce": "Purchasing: reduce", "turn_phase_out": "Purchasing: phase out", "festival": "Festival",
               "broken_set": "Broken set", "discount": "Discount control", "pause_buying": "Pause buying", "buy_band": "Buy"}


def _r(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v)) if abs(v) >= 100 else round(float(v), 3)
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    return v


def _week(lines, start, end):
    w = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= end)]
    return {"net_sales_rupees": _r(w["net_value"].sum()), "net_pieces": _r(w["net_pcs"].sum()), "bills": int(w["bill_no"].nunique())}


def _audit(audit):
    """The latest accuracy audit: counts, and which checks failed (by layer), so the brief can say what they mean."""
    if not audit:
        return None
    res = audit.results if isinstance(audit.results, list) else json.loads(audit.results or "[]")
    fails = [r for r in res if r.get("result") == "FAIL"]
    layers = {1: "loads vs the ERP's own totals", 2: "data sources compared with each other", 3: "stock flow continuity"}
    return {"passed": audit.passed, "failed": audit.failed, "at": str(audit.at)[:16],
            "loads_match_erp_exactly": not any(r.get("layer") == 1 and "load" in str(r.get("check", "")).lower() for r in fails),
            "failed_checks": sorted({f"{layers.get(r.get('layer'), r.get('layer'))}: {r.get('check')}" for r in fails}),
            "note": "Most failures are known gaps listed on the Data quality page (missing old snapshots, order-booked sales "
                    "not yet re-pulled, unexplained Stock Outward exits); a failure in loads vs the ERP would be new."}


def facts():
    """Everything the brief may say, computed from the database."""
    with settings.app_engine().connect() as conn:
        h = headline(conn)
        if not h:
            raise ai.AIError("No stock data to write a brief from.")
        as_of = pd.Timestamp(h["as_of"])
        series = h["series"]
        week_ago = series[series["snapshot_date"] <= as_of - pd.Timedelta(days=7)]
        lines = m.net_sales(q.sales_lines(conn, (as_of - pd.Timedelta(days=400)).date()))
        acts = pd.read_sql(text("""SELECT action_id, kind, title, impact_value, impact_label, status, branch, priority,
                                          first_seen_at, decided_at FROM brain.action"""), conn)
        stock = q.stock_latest(conn)
        fests = q.festivals(conn)
        audit = conn.execute(text("SELECT passed, failed, at, results FROM brain.audit_run ORDER BY audit_id DESC LIMIT 1")).first()
        note = notice.get(conn)
        followed = outcomes.follow(conn)
    end = lines["sale_date"].max() if len(lines) else as_of
    this_wk = _week(lines, end - pd.Timedelta(days=6), end)
    prev_wk = _week(lines, end - pd.Timedelta(days=13), end - pd.Timedelta(days=7))
    ly_wk = _week(lines, end - pd.Timedelta(days=371), end - pd.Timedelta(days=365))
    by_group = lines[lines["sale_date"] > end - pd.Timedelta(days=7)].groupby("item_class_group")["net_value"].sum()

    acts["first_seen_at"] = pd.to_datetime(acts["first_seen_at"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    acts["decided_at"] = pd.to_datetime(acts["decided_at"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    open_ = acts[acts["status"] == "open"].sort_values("priority", ascending=False)
    week_start = pd.Timestamp.now() - pd.Timedelta(days=7)
    stock = stock.assign(value=m.value(stock), where=m.location_type(stock["location"]))
    stale = stock[(stock["where"] == "Approval") & (stock["days_from_branch_in"] > 90)]

    return {
        "as_of": as_of.date().isoformat(), "sales_to": _r(end),
        "stock": {"value_rupees": _r(h["stock_value"]), "pieces": _r(h["pcs"]), "labels": h["labels"],
                  "value_a_week_earlier": _r(week_ago["value"].iloc[-1]) if len(week_ago) else None,
                  "aged_share": _r(h["aged_share"]), "aged_days": h["aged_days"], "turn_a_year": _r(h["turn"])},
        "sales": {"this_financial_year_rupees": _r(h["net_sales_fy"]), "last_7_days": this_wk, "previous_7_days": prev_wk,
                  "same_7_days_last_year": ly_wk,
                  "last_7_days_by_item_group": {k: _r(v) for k, v in by_group.sort_values(ascending=False).items()}},
        "recommendations": {
            "open": int(len(open_)), "open_and_new_this_week": int((open_["first_seen_at"] >= week_start).sum()),
            "decided_this_week": int((acts["decided_at"] >= week_start).sum()),
            "open_by_kind": {KIND_LABELS.get(k, k): int(n) for k, n in open_["kind"].value_counts().items()},
            "top_open": [{"id": int(r.action_id), "kind": KIND_LABELS.get(r.kind, r.kind), "title": r.title,
                          "money": r.impact_label, "branch": r.branch.split(" / ")[0] if isinstance(r.branch, str) else None}
                         for r in open_.head(12).itertuples()]},
        "outcomes_of_decisions": json.loads(outcomes.by_kind(followed).to_json(orient="records")) if len(followed) else [],
        "watch": {"approvals_out_over_90_days": {"pieces": int(len(stale)), "value_rupees": _r(stale["value"].sum())},
                  "festivals_next_60_days": [{"name": f.name, "day": f.day.date().isoformat(), "days_away": int((f.day - as_of).days)}
                                             for f in fests[(fests["day"] >= as_of) & (fests["day"] <= as_of + pd.Timedelta(days=60))].itertuples()]},
        "data": {"notice": (note or {}).get("text") if isinstance(note, dict) else None,
                 "last_accuracy_audit": _audit(audit)},
    }


SYSTEM = """You write the weekly one-page brief for the management of a diamond jewellery retailer in India.
The reader is the director: busy, numerate, wants decisions, not data dumps.

Use only the figures in the facts JSON you are given. Do not compute new figures beyond simple differences and
percentages of figures that are there, and never invent numbers, causes or names. If something important is
missing or a data notice is set, say so in one line.

Format (Markdown, under 400 words):
## The week in one line
## Numbers that moved   (3-5 bullets: stock, sales vs last week and vs the same week last year, ageing)
## Decisions this week   (the 3-5 most valuable open recommendations, each: what to do, the money involved, why
now; refer to them as "Recommendation #id")
## Watch                 (festivals coming, stale approvals, anything off-pattern)
## Data                  (one line on data quality; omit the section if there is nothing to say)

Money in Indian style with ₹, lakh (L) and crore (Cr). Values are before GST. Plain words, no jargon."""


def write(who="system"):
    """Compute the facts, have Claude write the brief, store it. Returns the text_id."""
    f = facts()
    msg, call_id = ai.call("ai_brief", who, max_tokens=8000, system=SYSTEM, output_config={"effort": "high"},
                           messages=[{"role": "user", "content": "Facts for this week's brief:\n" + ai.as_json(f)}])
    body = ai.text_of(msg)
    period = f"week to {pd.Timestamp(f['sales_to']):%d %b %Y}"
    with settings.app_engine().begin() as conn:
        return conn.execute(text("""INSERT INTO ai.text (kind, created_by, period, body_md, facts, call_id)
                                    VALUES ('brief', :w, :p, :b, CAST(:f AS jsonb), :c) RETURNING text_id"""),
                            {"w": who, "p": period, "b": body, "f": json.dumps(f, default=str), "c": call_id}).scalar_one()


def latest(kind="brief", n=12):
    with settings.app_engine().connect() as conn:
        return pd.read_sql(text("""SELECT t.text_id, t.created_at AT TIME ZONE 'Asia/Kolkata' AS created_at, t.created_by,
                                          t.period, t.body_md, t.facts, c.cost_usd
                                   FROM ai.text t LEFT JOIN ai.call_log c USING (call_id)
                                   WHERE t.kind = :k ORDER BY t.text_id DESC LIMIT :n"""), conn, params={"k": kind, "n": n})


if __name__ == "__main__":
    from kkj import features
    ap = argparse.ArgumentParser(description="Write this week's management brief")
    ap.add_argument("--facts-only", action="store_true", help="print the facts, don't call Claude")
    a = ap.parse_args()
    if a.facts_only:
        print(json.dumps(facts(), indent=2, ensure_ascii=False, default=str))
    elif not features.on("ai_brief"):
        print("The weekly brief is switched off (FEATURE_AI_BRIEF in .env) or there is no ANTHROPIC_API_KEY.")
    else:
        print("brief", write("scheduled"))
