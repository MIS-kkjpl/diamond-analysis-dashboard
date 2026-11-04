"""
Refresh the decisions: read the latest stock, recent sales and the business rules, run every
generator, and merge the result into brain.action (kkj.brain.store lifecycle). Logged in
brain.run_log. Runs after each data load, and from the dashboard's "Refresh decisions".

Run:  python -m kkj.brain.run [--trigger manual|pipeline|dashboard]
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
from kkj.metrics import core as m
from kkj.metrics import insights as ins

log = logging.getLogger("kkj.brain")


def candidates(conn, rules):
    """Every generator over the current data; returns (candidates, stock_as_of, sales_to)."""
    stock = m.company_stock(q.stock_latest(conn))
    if stock.empty:
        return [], None, None
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
    return actions, as_of.date(), sales["sale_date"].max().date() if len(sales) else None


def refresh(trigger="manual"):
    """One full refresh. Returns the run's counts."""
    engine = settings.app_engine()
    with engine.begin() as conn:
        run_id = conn.execute(text("INSERT INTO brain.run_log (trigger) VALUES (:t) RETURNING run_id"),
                              {"t": trigger}).scalar_one()
    try:
        with engine.begin() as conn:
            rules = rules_mod.load(conn)
            cands, as_of, sales_to = candidates(conn, rules)
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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = refresh(args.trigger)
    print(f"Run {result['run_id']}: {result['candidates']} candidates; created {result['created']}, "
          f"updated {result['updated']}, reopened {result['reopened']}, resolved {result['resolved']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
