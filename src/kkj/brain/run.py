"""
Refresh the decisions: read the latest stock, recent sales and the business rules, run every
generator, and merge the result into brain.action (kkj.brain.store lifecycle). Logged in
brain.run_log. Runs after each data load, and from the dashboard's "Refresh decisions".

Run:  python -m kkj.brain.run [--trigger manual|pipeline|dashboard] [--no-audit]
      (the accuracy audit runs after the refresh unless --no-audit)
"""
import argparse
import logging
import sys
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj import settings
from kkj.brain import generators as gen
from kkj.brain import store
from kkj.db import queries as q
from kkj.metrics import brick_turn as bt
from kkj.metrics import core as m
from kkj.metrics import insights as ins

log = logging.getLogger("kkj.brain")


def brick_turns(conn, rules, as_of, showrooms):
    """Weighted stock turn per brick and per brick group, for the company and each showroom.

    Returns one DataFrame with scope ('Company' or a branch) and level ('brick' or 'group'); for groups
    the 'brick' column holds the group key.
    """
    dates = q.stock_dates(conn)
    grid = bt.fortnight_grid(as_of, dates.loc[dates["snapshot_date"] <= as_of, "snapshot_date"])
    stock = bt.add_brick(q.brick_stock(conn, [s for _, s in grid if s is not None]), rules)
    sales = bt.add_brick(q.brick_sales(conn, (as_of - timedelta(days=bt.FORTNIGHT * bt.WINDOWS["12m"])).date()), rules)
    ideals = q.ideal_turns(conn)
    shop_stock, shop_sales = stock[stock["branch"].isin(showrooms)], sales[sales["branch"].isin(showrooms)]
    parts = []
    for level, name in (("brick", "brick"), ("brick_group", "group")):
        company = bt.compute(stock, sales, as_of, rules, level=level, ideals=ideals).assign(scope="Company")
        branch = bt.compute(shop_stock, shop_sales, as_of, rules, level=level, by=("branch",), ideals=ideals)             .rename(columns={"branch": "scope"})
        for df in (company, branch):
            if level == "brick_group":
                df = df.assign(brick=df["brick_group"])
            parts.append(df.assign(level=name))
    return pd.concat(parts, ignore_index=True)


def save_brick_turns(conn, table, as_of, run_id):
    conn.execute(text("DELETE FROM brain.brick_turn WHERE as_of = :a"), {"a": as_of})
    cols = [r[0] for r in conn.execute(text("""SELECT column_name FROM information_schema.columns
                                                WHERE table_schema = 'brain' AND table_name = 'brick_turn'"""))]
    df = table.assign(as_of=as_of, run_id=run_id)
    df[[c for c in cols if c in df]].to_sql("brick_turn", conn, schema="brain", if_exists="append", index=False,
                                             method="multi", chunksize=1000)


def candidates(conn, rules):
    """Every generator over the current data; returns (candidates, stock_as_of, sales_to, brick turn table)."""
    stock = m.company_stock(q.stock_latest(conn))
    if stock.empty:
        return [], None, None, None
    stock = stock.assign(value=m.value(stock))
    as_of = pd.Timestamp(stock["snapshot_date"].iloc[0])
    window_start = (as_of - timedelta(days=rules["demand_window_days"])).date()
    sales = q.sales_lines(conn, window_start)
    sales = sales[(sales["pcs"] > 0) & (sales["sale_date"] <= as_of)]
    branches = q.branches(conn)
    showrooms = branches.loc[(branches["kind"] == "showroom") & branches["active"], "erp_branch"].tolist()
    people = q.salespeople(conn)

    actions = gen.transfers_and_understock(stock, sales, rules, showrooms)
    moving = {p["barcode"] for a in actions if a["kind"] == "transfer" for p in a["pieces"]}
    actions += gen.offers(stock, sales, rules, showrooms, exclude_barcodes=moving)
    actions += gen.liquidation(stock, rules)
    actions += gen.buying(stock, sales, rules)
    actions += gen.discounts(stock, sales, rules, people)

    # suppliers: judged over the longer insight period, price-band adjusted
    start = (as_of - pd.DateOffset(months=rules["insight_months"])).date()
    history = ins.one_per_week(q.stock_history(conn, start))
    h_start, h_end = history["snapshot_date"].min(), history["snapshot_date"].max()
    long_sales = q.sales_lines(conn, h_start.date())
    long_sales = long_sales[(long_sales["pcs"] > 0) & (long_sales["sale_date"] <= h_end)]
    long_sales = long_sales.assign(value=m.value(long_sales), age_at_sale=long_sales["age_days_at_sale"])
    bands = [0] + list(rules["price_band_edges"]) + [float("inf")]
    card = ins.supplier_scorecard(history, long_sales, h_start, h_end, bands, rules["min_supplier_pcs"])
    actions += gen.suppliers(card, stock)

    turns = brick_turns(conn, rules, as_of, showrooms)
    actions += gen.stock_turn(turns, rules)
    return actions, as_of.date(), sales["sale_date"].max().date() if len(sales) else None, turns


def refresh(trigger="manual"):
    """One full refresh. Returns the run's counts."""
    engine = settings.app_engine()
    with engine.begin() as conn:
        run_id = conn.execute(text("INSERT INTO brain.run_log (trigger) VALUES (:t) RETURNING run_id"),
                              {"t": trigger}).scalar_one()
    try:
        with engine.begin() as conn:
            rules = rules_mod.load(conn)
            cands, as_of, sales_to, turns = candidates(conn, rules)
            if turns is not None:
                save_brick_turns(conn, turns, as_of, run_id)
            existing = q.read(conn, "SELECT action_key, status, snoozed_until FROM brain.action")
            counts = store.apply(conn, store.reconcile(existing, cands, date.today()), run_id)
            conn.execute(text("""UPDATE brain.run_log SET finished_at = now(), status = 'success', stock_as_of = :a,
                                 sales_to = :s, candidates = :c, created = :cr, updated = :u, resolved = :r,
                                 reopened = :ro WHERE run_id = :id"""),
                         {"a": as_of, "s": sales_to, "c": len(cands), "cr": counts["created"], "u": counts["updated"],
                          "r": counts["resolved"], "ro": counts["reopened"], "id": run_id})
        return {"run_id": run_id, "candidates": len(cands), **counts}
    except Exception as e:
        with engine.begin() as conn:
            conn.execute(text("UPDATE brain.run_log SET finished_at = now(), status = 'failed', error = :e "
                              "WHERE run_id = :id"), {"e": f"{type(e).__name__}: {e}"[:2000], "id": run_id})
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refresh the decisions (brain.action).")
    ap.add_argument("--trigger", default="manual", choices=["manual", "pipeline", "dashboard"])
    ap.add_argument("--no-audit", action="store_true", help="skip the accuracy audit after the refresh")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = refresh(args.trigger)
    print(f"Run {result['run_id']}: {result['candidates']} candidates; created {result['created']}, "
          f"updated {result['updated']}, reopened {result['reopened']}, resolved {result['resolved']}")
    if not args.no_audit:              # a failed audit is reported, never fails the refresh
        try:
            from kkj.checks.audit import audit_and_save
            _, _, counts = audit_and_save()
            print(f"Audit: {counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, {counts.get('INFO', 0)} info")
        except Exception as e:
            print(f"Audit could not run: {type(e).__name__}: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
