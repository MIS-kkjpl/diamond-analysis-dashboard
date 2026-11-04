"""
Decision lifecycle: merge a fresh set of candidate decisions into the stored ones.

A decision keeps its identity (action_key) across refreshes. On every refresh:
  new key                          -> created (open)
  still proposed, open/approved    -> numbers updated
  still proposed, snoozed          -> updated; reopens once the snooze has passed
  still proposed, rejected         -> numbers updated, stays rejected (a person said no)
  proposed again after resolved/done -> reopened (the problem came back)
  no longer proposed, open/snoozed -> resolved (the situation cleared by itself)
  no longer proposed, approved     -> done (the approved action's problem is gone)
reconcile() decides this without touching the database; apply() writes it in one
transaction with an event per change.
"""
import json
import math

import pandas as pd
from sqlalchemy import text

ACTIVE = ("open", "approved", "snoozed")


def reconcile(existing, candidates, today):
    """existing: DataFrame(action_key, status, snoozed_until); candidates: list of dicts with action_key.

    Returns {"create": [cand], "update": [cand], "reopen": [key], "snooze_end": [key],
             "resolve": [key], "done": [key]}.
    """
    by_key = existing.set_index("action_key") if len(existing) else pd.DataFrame(columns=["status", "snoozed_until"])
    seen = set()
    ops = {"create": [], "update": [], "reopen": [], "snooze_end": [], "resolve": [], "done": []}
    for c in candidates:
        key = c["action_key"]
        if key in seen:
            continue
        seen.add(key)
        if key not in by_key.index:
            ops["create"].append(c)
            continue
        ops["update"].append(c)
        status = by_key.at[key, "status"]
        if status in ("resolved", "done"):
            ops["reopen"].append(key)
        elif status == "snoozed":
            until = by_key.at[key, "snoozed_until"]
            if until is None or pd.isna(until) or pd.Timestamp(until).date() <= today:
                ops["snooze_end"].append(key)
    for key, row in by_key.iterrows():
        if key in seen:
            continue
        if row["status"] in ("open", "snoozed"):
            ops["resolve"].append(key)
        elif row["status"] == "approved":
            ops["done"].append(key)
    return ops


_FIELDS = ("kind", "title", "summary", "branch", "subject", "impact_value", "impact_label", "priority",
           "confidence", "evidence", "pieces")


def _plain(value):
    """JSON-safe copy: numpy numbers -> Python, infinity/NaN -> None (JSON has neither)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _params(c, run_id):
    p = {f: c.get(f) for f in _FIELDS}
    for f in ("subject", "evidence", "pieces"):
        p[f] = json.dumps(_plain(p[f]), default=str) if p[f] is not None else None
    p.update(key=c["action_key"], run=run_id)
    return p


def apply(conn, ops, run_id):
    """Write the reconcile() result; returns counts. Caller owns the transaction."""
    for c in ops["create"]:
        action_id = conn.execute(text("""
            INSERT INTO brain.action (action_key, kind, title, summary, branch, subject, impact_value, impact_label,
                                      priority, confidence, evidence, pieces, first_run_id, last_run_id, first_impact)
            VALUES (:key, :kind, :title, :summary, :branch, CAST(:subject AS jsonb), :impact_value, :impact_label,
                    :priority, :confidence, CAST(:evidence AS jsonb), CAST(:pieces AS jsonb), :run, :run, :impact_value)
            RETURNING action_id"""), _params(c, run_id)).scalar_one()
        _event(conn, action_id, "created", data={"impact": c.get("impact_value")})
    for c in ops["update"]:
        conn.execute(text("""
            UPDATE brain.action SET kind = :kind, title = :title, summary = :summary, branch = :branch,
                   subject = CAST(:subject AS jsonb), impact_value = :impact_value, impact_label = :impact_label,
                   priority = :priority, confidence = :confidence, evidence = CAST(:evidence AS jsonb),
                   pieces = CAST(:pieces AS jsonb), last_seen_at = now(), last_run_id = :run,
                   times_seen = times_seen + 1
            WHERE action_key = :key"""), _params(c, run_id))
    for event, keys, status in (("reopened", ops["reopen"], "open"), ("snooze_ended", ops["snooze_end"], "open"),
                                ("auto_resolved", ops["resolve"], "resolved"),
                                ("condition_cleared", ops["done"], "done")):
        for key in keys:
            action_id = conn.execute(text("""
                UPDATE brain.action SET status = :status, snoozed_until = NULL WHERE action_key = :key
                RETURNING action_id"""), {"status": status, "key": key}).scalar_one()
            _event(conn, action_id, event)
    return {"created": len(ops["create"]), "updated": len(ops["update"]), "reopened": len(ops["reopen"]),
            "resolved": len(ops["resolve"]) + len(ops["done"])}


def _event(conn, action_id, event, by="system", note=None, data=None):
    conn.execute(text("""INSERT INTO brain.action_event (action_id, by, event, note, data)
                         VALUES (:a, :by, :e, :n, CAST(:d AS jsonb))"""),
                 {"a": action_id, "by": by, "e": event, "n": note, "d": json.dumps(data, default=str) if data else None})


DECISIONS = {"approve": "approved", "reject": "rejected", "snooze": "snoozed", "done": "done", "reopen": "open"}


def decide(conn, action_id, decision, by, note=None, snooze_days=7):
    """A person's decision on one action (approve / reject / snooze / done / reopen) or a comment."""
    if decision == "comment":
        _event(conn, action_id, "comment", by=by, note=note)
        return
    status = DECISIONS[decision]
    conn.execute(text("""
        UPDATE brain.action SET status = :s, decided_at = now(), decided_by = :by, decision_note = :note,
               snoozed_until = CASE WHEN :s = 'snoozed' THEN current_date + CAST(:days AS int) ELSE NULL END
        WHERE action_id = :a"""), {"s": status, "by": by, "note": note, "days": snooze_days, "a": action_id})
    _event(conn, action_id, {"open": "reopened"}.get(status, status), by=by, note=note,
           data={"snooze_days": snooze_days} if status == "snoozed" else None)
