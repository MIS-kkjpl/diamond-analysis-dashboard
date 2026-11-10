"""
Ideals fixed by hand (config.ideal_stock_override): the people half of the ideal-stock loop.

The system calculates an ideal for every brick group at every branch (kkj.metrics.ideal_stock). Management can fix
it for a brick group or a whole product, at a branch or all branches, with a reason. Each fix records what the
system calculated at that moment (ideal, 12-month forecast, units held) and a review date (override_review_days).
On that date it comes back as a recommendation (kkj.brain.generators.ideal_reviews) with what sold since, to keep,
change or remove; and fixes that point the same way within a product become a suggested rule change on the Ideal
stock page. So the engine corrects itself, with people supplying what the data cannot know.
"""
import json
from datetime import date, timedelta

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
