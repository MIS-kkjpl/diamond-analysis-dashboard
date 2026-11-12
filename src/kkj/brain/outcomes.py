"""
What happened after a recommendation was decided: did the pieces sell or move, did the brick group's stock and
turn change? Approved and dismissed recommendations are followed the same way, so the two can be compared
(a dismissed markdown whose pieces sold anyway says something too).

  pieces(conn, actions)   one row per piece of piece-based recommendations: sold / moved / where it was / left stock
  groups(conn, actions)   purchasing recommendations: brick group stock and turn when decided vs now
  follow(conn, ids=None)  one row per decided recommendation with a plain-language outcome
"""
import json

import pandas as pd
from sqlalchemy import text

from kkj.scope import own_goods

DECIDED = ("approved", "done", "rejected")
PIECE_KINDS = ("transfer", "offer", "liquidate", "broken_set", "understock")
GROUP_KINDS = ("turn_increase", "turn_reduce", "turn_phase_out")


def _read(conn, sql, **params):
    return pd.read_sql(text(sql), conn, params=params)


def decided(conn, ids=None):
    where = "AND action_id = ANY(:ids)" if ids else ""
    df = _read(conn, f"""SELECT action_id, kind, title, status, branch, subject, pieces, decided_at, decided_by
                         FROM brain.action WHERE status = ANY(:st) AND decided_at IS NOT NULL {where}""",
               st=list(DECIDED), ids=list(ids or []))
    df["decided_at"] = pd.to_datetime(df["decided_at"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df


def _as_obj(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def pieces(conn, actions):
    """Each piece of a piece-based recommendation, followed from the day it was decided."""
    rows = [{"action_id": a.action_id, "decided_at": a.decided_at, **p}
            for a in actions.itertuples() if a.kind in PIECE_KINDS for p in (_as_obj(a.pieces) or [])]
    if not rows:
        return pd.DataFrame(columns=["action_id", "barcode", "outcome", "sale_date", "sold_value", "now_where"])
    p = pd.DataFrame(rows)
    codes = p["barcode"].dropna().unique().tolist()
    now = _read(conn, f"""SELECT barcode, branch, location FROM erp.stock_snapshot
                          WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot) AND barcode = ANY(:b)""",
                b=codes)
    sold = _read(conn, f"""SELECT barcode, sale_date, bill_no, coalesce(taxable_amount, item_amt) AS sold_value
                           FROM sales.v_sales_line WHERE pcs > 0 AND barcode = ANY(:b) AND {own_goods()}""",
                 b=codes, **_excl(conn))
    sold["sale_date"] = pd.to_datetime(sold["sale_date"])
    p = p.merge(now.rename(columns={"branch": "now_branch", "location": "now_location"}), on="barcode", how="left")
    s = p[["action_id", "barcode", "decided_at"]].merge(sold, on="barcode")
    s = s[s["sale_date"] >= s["decided_at"].dt.normalize()].sort_values("sale_date").drop_duplicates(["action_id", "barcode"])
    p = p.merge(s[["action_id", "barcode", "sale_date", "bill_no", "sold_value"]], on=["action_id", "barcode"], how="left")
    was = p["location"].fillna("") if "location" in p else pd.Series("", index=p.index)
    p["now_where"] = (p["now_branch"].fillna("").str.split(" / ").str[0] + " · " + p["now_location"].fillna("")).where(
        p["now_location"].notna(), None)
    p["outcome"] = "unchanged"
    p.loc[p["now_location"].notna() & (p["now_location"] != was), "outcome"] = "moved"
    p.loc[p["now_location"].isna(), "outcome"] = "left stock, no sale"
    from kkj.scope import excluded_locations
    p.loc[p["now_location"].isin(excluded_locations(conn)), "outcome"] = "delabelled"
    p.loc[p["sale_date"].notna(), "outcome"] = "sold"
    return p


def _excl(conn):
    from kkj.scope import excluded
    g, prod = excluded(conn)
    return {"x_groups": g, "x_products": prod}


def groups(conn, actions):
    """Purchasing recommendations: the brick group's company stock and turn on the refresh before the decision vs
    the latest refresh."""
    a = actions[actions["kind"].isin(GROUP_KINDS)].copy()
    if a.empty:
        return pd.DataFrame(columns=["action_id", "pcs_then", "pcs_now", "turn_then", "turn_now", "ideal_pcs_then"])
    a["group"] = a["subject"].map(lambda s: _as_obj(s).get("group"))
    t = _read(conn, """SELECT as_of, brick, current_pcs, blended_turn, ideal_pcs FROM brain.brick_turn
                       WHERE scope = 'Company' AND level = 'group' AND brick = ANY(:g)""", g=a["group"].dropna().tolist())
    t["as_of"] = pd.to_datetime(t["as_of"])
    out = []
    for r in a.itertuples():
        rows = t[t["brick"] == r.group].sort_values("as_of")
        then = rows[rows["as_of"] <= r.decided_at]
        then = then.iloc[-1] if len(then) else (rows.iloc[0] if len(rows) else None)
        now = rows.iloc[-1] if len(rows) else None
        out.append({"action_id": r.action_id,
                    "pcs_then": None if then is None else then["current_pcs"],
                    "pcs_now": None if now is None else now["current_pcs"],
                    "turn_then": None if then is None else then["blended_turn"],
                    "turn_now": None if now is None else now["blended_turn"],
                    "ideal_pcs_then": None if then is None else then["ideal_pcs"]})
    return pd.DataFrame(out)


def follow(conn, ids=None):
    """One row per decided recommendation: days since the decision and what happened."""
    acts = decided(conn, ids)
    if acts.empty:
        return acts.assign(days=None, outcome=None, sold=0, sold_value=0.0, n_pieces=0)
    today = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    acts["days"] = (today - acts["decided_at"]).dt.days
    p = pieces(conn, acts)
    g = groups(conn, acts).set_index("action_id") if len(acts) else pd.DataFrame()
    rows = []
    for a in acts.itertuples():
        mine = p[p["action_id"] == a.action_id] if len(p) else p
        rec = {"action_id": a.action_id, "n_pieces": len(mine), "sold": int((mine["outcome"] == "sold").sum()) if len(mine) else 0,
               "sold_value": float(mine["sold_value"].sum()) if len(mine) else 0.0}
        if len(mine):
            counts = mine["outcome"].value_counts()
            text_ = ", ".join(f"{n} {o}" for o, n in counts.items())
            rec["outcome"] = f"{text_} (of {len(mine)} pieces)"
        elif a.action_id in g.index and pd.notna(g.loc[a.action_id, "pcs_now"]):
            r = g.loc[a.action_id]
            turn = (f"; turn {r.turn_then:.2f}× → {r.turn_now:.2f}×" if pd.notna(r.turn_then) and pd.notna(r.turn_now) else "")
            rec["outcome"] = f"stock {r.pcs_then:.0f} → {r.pcs_now:.0f} (ideal then {r.ideal_pcs_then:.0f}){turn}"
        else:
            rec["outcome"] = "not followed for this kind yet"
        rows.append(rec)
    return acts.merge(pd.DataFrame(rows), on="action_id")


def by_kind(followed):
    """Approved vs dismissed, per kind: how many, and the share of their pieces that sold since."""
    if followed.empty:
        return pd.DataFrame(columns=["kind", "decision", "recommendations", "pieces", "sold", "sold_share", "sold_value"])
    f = followed.assign(decision=followed["status"].map({"approved": "approved", "done": "approved", "rejected": "dismissed"}))
    t = f.groupby(["kind", "decision"], as_index=False).agg(recommendations=("action_id", "size"), pieces=("n_pieces", "sum"),
                                                           sold=("sold", "sum"), sold_value=("sold_value", "sum"))
    t["sold_share"] = t["sold"] / t["pieces"].where(t["pieces"] > 0)
    return t[["kind", "decision", "recommendations", "pieces", "sold", "sold_share", "sold_value"]]
