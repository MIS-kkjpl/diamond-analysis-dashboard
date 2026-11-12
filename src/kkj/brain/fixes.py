"""
Ideals fixed by hand (config.ideal_stock_override): the people half of the ideal-stock loop.

The system calculates an ideal for every brick group at every branch (kkj.metrics.ideal_stock). A person can fix
it for a brick group or a whole product, at a branch or all branches, with a reason. Each fix records what the
system calculated at that moment (ideal, 12-month forecast, units held) and a review date (override_review_days).
On that date it comes back as a recommendation (kkj.brain.generators.ideal_reviews) with what sold since, to keep,
change or remove; and fixes that point the same way within a product become a suggested rule change on the Ideal
stock page. So the engine corrects itself, with people supplying what the data cannot know.
"""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

FIX_LEVELS = {"group": "Brick group", "product": "Product"}
COLUMN = {"group": "brick_group", "product": "item_name"}


def calculated(conn, level, key, branch):
    """(calculated ideal, 12-month forecast, units held) in the latest ideal stock for this group/product at this
    branch ('*' = all branches), before any fix."""
    row = conn.execute(text(f"""
        SELECT sum(coalesce(calc_pcs, ideal_pcs)), sum(annual), sum(current_pcs) FROM brain.ideal_stock
        WHERE as_of = (SELECT max(as_of) FROM brain.ideal_stock) AND scope <> 'Company'
          AND {COLUMN[level]} = :k AND (:b = '*' OR scope = :b)"""), {"k": key, "b": branch}).one()
    return tuple(float(x) if x is not None else None for x in row)


def where(branch):
    return "all branches" if branch == "*" else branch


def save(conn, level, key, branch, pcs, reason, by, review_days):
    """Fix an ideal (insert or replace). Returns (message, None) or (None, error). Caller commits."""
    reason = (reason or "").strip()
    if level not in FIX_LEVELS or not key or not branch:
        return None, "Choose a brick group or product, and a branch (or all branches)."
    if not reason:
        return None, "A fixed ideal needs a reason, so the correction stays visible."
    try:
        pcs = int(pcs)
        if not 0 <= pcs <= 100_000:
            raise ValueError
    except (TypeError, ValueError):
        return None, "Pieces: a whole number from 0."
    calc, annual, held = calculated(conn, level, key, branch)
    old = conn.execute(text("SELECT pcs FROM config.ideal_stock_override WHERE level = :l AND key = :k AND branch = :b"),
                       {"l": level, "k": key, "b": branch}).scalar()
    review = date.today() + timedelta(days=int(review_days))
    conn.execute(text("""
        INSERT INTO config.ideal_stock_override (level, key, branch, pcs, reason, updated_by, review_on, calc_at_fix,
                                                 annual_at_fix, held_at_fix)
        VALUES (:l, :k, :b, :p, :r, :by, :rv, :c, :a, :h)
        ON CONFLICT (level, key, branch) DO UPDATE SET pcs = EXCLUDED.pcs, reason = EXCLUDED.reason, updated_at = now(),
            updated_by = EXCLUDED.updated_by, review_on = EXCLUDED.review_on, calc_at_fix = EXCLUDED.calc_at_fix,
            annual_at_fix = EXCLUDED.annual_at_fix, held_at_fix = EXCLUDED.held_at_fix"""),
        {"l": level, "k": key, "b": branch, "p": pcs, "r": reason, "by": by, "rv": review, "c": calc, "a": annual,
         "h": held})
    _log(conn, by, level, key, branch, old, pcs, reason)
    calc_txt = f" (calculated: {calc:.0f})" if calc is not None else ""
    split = ", split by the calculated ideals" if level == "product" or branch == "*" else ""
    return (f"{FIX_LEVELS[level]} {key} at {where(branch)}: ideal fixed at {pcs}{calc_txt}{split}. "
            f"Recommendations follow at the next refresh; it comes back for review on {review:%d %b %Y}."), None


def remove(conn, level, key, branch, by):
    old = conn.execute(text("SELECT pcs FROM config.ideal_stock_override WHERE level = :l AND key = :k AND branch = :b"),
                       {"l": level, "k": key, "b": branch}).scalar()
    conn.execute(text("DELETE FROM config.ideal_stock_override WHERE level = :l AND key = :k AND branch = :b"),
                 {"l": level, "k": key, "b": branch})
    _log(conn, by, level, key, branch, old, None, None)
    return f"Removed the fixed ideal for {key} at {where(branch)}; it is calculated again from the next refresh."


def _log(conn, by, level, key, branch, old, new, note):
    conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value, note)
                         VALUES (:by, 'ideal_stock_override', :k, CAST(:old AS jsonb), CAST(:new AS jsonb), :note)"""),
                 {"by": by, "k": f"{level}: {key} @ {where(branch)}", "old": json.dumps(old), "new": json.dumps(new),
                  "note": note})


def suggestions(overrides, latest, choice_months):
    """Rule changes the fixes point to: per product with at least two fixes all on the same side of the calculated
    ideal (by more than 20%), a choice cover scaled by their typical ratio. latest: brain.ideal_stock branch rows."""
    if overrides is None or not len(overrides) or latest is None or not len(latest):
        return []
    out = {}
    for o in overrides.itertuples():
        hit = latest[(latest[COLUMN[o.level]] == o.key) & ((latest["scope"] == o.branch) if o.branch != "*" else True)]
        calc = (hit["calc_pcs"].fillna(hit["ideal_pcs"])).astype(float).sum()
        if calc <= 0 or hit.empty:
            continue
        product = hit["item_name"].mode().iloc[0]
        out.setdefault(product, []).append((o.pcs / calc, o.reason))
    result = []
    for product, items in out.items():
        ratios = sorted(r for r, _ in items)
        if len(items) < 2 or not (all(r < 0.8 for r in ratios) or all(r > 1.25 for r in ratios)):
            continue
        typical = ratios[len(ratios) // 2]
        result.append({"product": product, "fixes": len(items), "ratio": typical,
                       "months": round(choice_months * typical, 1), "reasons": "; ".join(sorted({r for _, r in items}))})
    return sorted(result, key=lambda r: -r["fixes"])


def save_many(conn, level, changes, reason, by, rules):
    """Save several numbers typed on one page with one reason. changes: [(key, branch, pcs)], branch '*' = all
    branches. A number equal to the calculated ideal removes that fix (back to the suggestion). Then the fixes are
    applied to the latest ideal stock at once (apply_now). Returns (message, None) or (None, error). Caller commits."""
    reason = (reason or "").strip()
    if level not in FIX_LEVELS:
        return None, "Unknown level."
    if not changes:
        return None, "No changes to save."
    parsed = []
    for key, branch, pcs in changes:
        try:
            n = int(float(pcs))
            if not 0 <= n <= 100_000:
                raise ValueError
        except (TypeError, ValueError):
            return None, f"{key}: enter a whole number."
        parsed.append((key, branch or "*", n))
    saved, back = 0, 0
    for key, branch, n in parsed:
        calc = calculated(conn, level, key, branch)[0]
        if calc is not None and n == round(calc):
            exists = conn.execute(text("""SELECT 1 FROM config.ideal_stock_override
                                          WHERE level = :l AND key = :k AND branch = :b"""),
                                  {"l": level, "k": key, "b": branch}).scalar()
            if exists:
                remove(conn, level, key, branch, by)
                back += 1
            continue
        if not reason:
            return None, "Enter a reason."
        msg, err = save(conn, level, key, branch, n, reason, by, rules["override_review_days"])
        if err:
            return None, f"{key}: {err}"
        saved += 1
    apply_now(conn, rules)
    parts = []
    if saved:
        review = date.today() + timedelta(days=int(rules["override_review_days"]))
        parts.append(f"Saved {saved} change{'s' if saved != 1 else ''}, review on {review:%d %b %Y}")
    if back:
        parts.append(f"{back} reset to suggested")
    return "; ".join(parts) + ".", None


def reset(conn, level, key, branch, by, rules):
    """Back to the calculated ideal for one fix, applied at once."""
    remove(conn, level, key, branch, by)
    apply_now(conn, rules)
    place = "all branches" if branch == "*" else branch.split(" / ")[0]
    return f"Reset {key} at {place}."


def apply_now(conn, rules):
    """Re-apply every fix to the latest ideal stock (brain.ideal_stock) without re-forecasting: the calculated ideal
    (calc_pcs) stays, fixes go on top, and change / status / implied turn and the company rows follow. So a number
    typed in is what the pages show straight away; recommendations follow at the next refresh."""
    from kkj.metrics import ideal_stock as ist
    rows = pd.read_sql(text("""SELECT * FROM brain.ideal_stock
                               WHERE as_of = (SELECT max(as_of) FROM brain.ideal_stock)"""), conn)
    if rows.empty:
        return 0
    br = rows[rows["scope"] != "Company"].rename(columns={"scope": "branch"}).copy()
    if br.empty:
        return 0
    for c in ("current_pcs", "ideal_pcs", "calc_pcs", "annual", "for_demand", "display_min", "for_choice"):
        br[c] = pd.to_numeric(br[c], errors="coerce").astype(float)
    br["ideal_pcs"] = br["calc_pcs"].fillna(br["ideal_pcs"])
    br["ideal_from"] = br["calc_from"].where(br["calc_from"].notna(), br["ideal_from"])
    br.loc[br["ideal_pcs"] == 0, "ideal_from"] = "no demand"
    overrides = pd.read_sql(text("SELECT level, key, branch, pcs, reason FROM config.ideal_stock_override"), conn)
    ist.apply_overrides(br, overrides)
    br["change_pcs"] = br["ideal_pcs"] - br["current_pcs"]
    br["implied_turn"] = np.where(br["ideal_pcs"] > 0, br["annual"] / br["ideal_pcs"].where(br["ideal_pcs"] > 0), np.nan)
    br["status"] = ist.status(br["current_pcs"], br["ideal_pcs"], rules, ist.proven_dead(br, rules))
    comp = rows[rows["scope"] == "Company"].set_index("brick_group")
    agg = br.groupby("brick_group").agg(ideal_pcs=("ideal_pcs", "sum"), calc_pcs=("calc_pcs", "sum"),
                                        n_fixed=("override_pcs", lambda s: int(s.notna().sum())))
    agg = agg[agg.index.isin(comp.index)]
    cur = pd.to_numeric(comp.loc[agg.index, "current_pcs"], errors="coerce").astype(float)
    annual = pd.to_numeric(comp.loc[agg.index, "annual"], errors="coerce").astype(float)
    agg["change_pcs"] = agg["ideal_pcs"] - cur
    agg["implied_turn"] = np.where(agg["ideal_pcs"] > 0, annual / agg["ideal_pcs"].where(agg["ideal_pcs"] > 0), np.nan)
    agg["status"] = ist.status(cur, agg["ideal_pcs"], rules, ist.proven_dead(comp.loc[agg.index], rules))
    as_of = rows["as_of"].iloc[0]
    clean = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else (float(v) if isinstance(v, (int, float, np.number)) else v)
    old = rows.set_index(["scope", "brick_group"])
    params = []
    for r in br.itertuples():
        o = old.loc[(r.branch, r.brick_group)]
        new = (clean(r.ideal_pcs), r.ideal_from, clean(r.override_pcs), r.override_reason, r.status)
        if (clean(float(o["ideal_pcs"])), o["ideal_from"], clean(o["override_pcs"] if pd.notna(o["override_pcs"]) else None),
                o["override_reason"], o["status"]) != new:
            params.append({"a": as_of, "s": r.branch, "g": r.brick_group, "i": clean(r.ideal_pcs), "f": r.ideal_from,
                           "op": clean(r.override_pcs), "or": r.override_reason, "c": clean(r.change_pcs),
                           "t": clean(r.implied_turn), "st": r.status})
    for g, r in agg.iterrows():
        o = old.loc[("Company", g)]
        if (float(o["ideal_pcs"]), o["status"]) != (float(r["ideal_pcs"]), r["status"]):
            params.append({"a": as_of, "s": "Company", "g": g, "i": float(r["ideal_pcs"]), "f": None, "op": None,
                           "or": None, "c": clean(r["change_pcs"]), "t": clean(r["implied_turn"]), "st": r["status"]})
    if params:
        conn.execute(text("""UPDATE brain.ideal_stock SET ideal_pcs = :i, ideal_from = coalesce(:f, ideal_from),
                                    override_pcs = CASE WHEN scope = 'Company' THEN override_pcs ELSE :op END,
                                    override_reason = CASE WHEN scope = 'Company' THEN override_reason ELSE :or END,
                                    change_pcs = :c, implied_turn = :t, status = :st
                             WHERE as_of = :a AND scope = :s AND brick_group = :g"""), params)
    return len(params)
