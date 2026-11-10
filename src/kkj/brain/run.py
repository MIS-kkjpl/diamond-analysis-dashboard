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

import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj import settings
from kkj.brain import generators as gen
from kkj.brain import store
from kkj.db import queries as q
from kkj.metrics import brick_turn as bt
from kkj.metrics import core as m
from kkj.metrics import forecast as fc
from kkj.metrics import ideal_stock as ist
from kkj.metrics import insights as ins
from kkj.metrics import seasonality as sn
from kkj.metrics import sets

log = logging.getLogger("kkj.brain")


def seasonal_indexes(conn, rules):
    """Monthly indexes by product and by item group from the five-year history; (None, None) without it."""
    daily = q.hist_daily(conn)
    if daily.empty:
        return None, None
    return (sn.monthly_index(daily, "item_name", rules["season_min_year_pcs"]),
            sn.monthly_index(daily, "item_class_group", rules["season_min_year_pcs"]))


def seasonal_shares(conn, rules, as_of):
    """{"product": {...}, "group": {...}}: each stock-turn window's share of a typical year, by key."""
    by_product, by_group = seasonal_indexes(conn, rules)
    if by_product is None:
        return None
    windows = {name: bt.FORTNIGHT * n for name, n in bt.WINDOWS.items()}
    return {"product": sn.shares_for_windows(by_product, "item_name", as_of, windows),
            "group": sn.shares_for_windows(by_group, "item_class_group", as_of, windows)}


def brick_turns(conn, rules, as_of, showrooms):
    """Weighted stock turn per brick and per brick group, for the company and each showroom.

    Returns one DataFrame with scope ('Company' or a branch) and level ('brick' or 'group'); for groups
    the 'brick' column holds the group key.
    """
    dates = q.stock_dates(conn)
    grid = bt.fortnight_grid(as_of, dates.loc[dates["snapshot_date"] <= as_of, "snapshot_date"])
    # counted in selling units: a multi-label set is one unit of its set type (kkj.metrics.sets)
    stock = bt.add_brick(sets.stock_units(q.brick_stock(conn, [s for _, s in grid if s is not None])), rules)
    sales = bt.add_brick(sets.sales_units(q.brick_sales(conn, (as_of - timedelta(days=bt.FORTNIGHT * bt.WINDOWS["12m"])).date())),
                         rules)
    ideals = q.ideal_turns(conn)
    season = seasonal_shares(conn, rules, as_of) if rules["turn_seasonal_adjust"] else None
    shop_stock, shop_sales = stock[stock["branch"].isin(showrooms)], sales[sales["branch"].isin(showrooms)]
    parts = []
    for level, name in (("brick", "brick"), ("brick_group", "group")):
        company = bt.compute(stock, sales, as_of, rules, level=level, ideals=ideals, season=season) \
            .assign(scope="Company")
        branch = bt.compute(shop_stock, shop_sales, as_of, rules, level=level, by=("branch",), ideals=ideals,
                            season=season).rename(columns={"branch": "scope"})
        for df in (company, branch):
            if level == "brick_group":
                df = df.assign(brick=df["brick_group"])
            parts.append(df.assign(level=name))
    return pd.concat(parts, ignore_index=True)


FORECAST_FROM = date(2021, 4, 1)          # the start of the ERP sales history
ACCURACY_LEVELS = {"company": [], "item_group": ["item_class_group"], "product": ["item_name"],
                   "product_branch": ["item_name", "branch"], "group_branch": ["brick_group", "branch"]}


def forecast_dates(dates, as_of):
    """Stock dates for the forecast panel: per month the one nearest mid-month and the last, plus as_of (two
    readings a month are enough for availability, and keep five years of snapshots quick to read)."""
    d = pd.DataFrame({"d": pd.to_datetime(pd.Series(list(dates)))})
    d = d[d["d"] <= as_of]
    pick = {pd.Timestamp(as_of)}
    for month, g in d.groupby(d["d"].dt.to_period("M")):
        pick.add(g["d"].max())
        mid = month.start_time + pd.Timedelta(days=14)
        pick.add(g.loc[(g["d"] - mid).abs().idxmin(), "d"])
    return sorted(pick)


def demand_and_ideal(conn, rules, as_of):
    """Demand forecast, its accuracy test and the ideal stock (kkj.metrics.forecast / ideal_stock).

    Returns {"branch": rows per (brick group, branch), "company": rows per brick group, "accuracy": rows per
    level and key}; empty frames without enough history."""
    as_of = pd.Timestamp(as_of)
    dates = q.stock_dates(conn)
    stock = bt.add_brick(sets.stock_units(q.brick_stock(conn, forecast_dates(dates["snapshot_date"], as_of))), rules)
    sales = bt.add_brick(sets.sales_units(q.brick_sales(conn, FORECAST_FROM)), rules)
    sales = sales[sales["sale_date"] <= as_of]
    last = as_of.to_period("M") - 1                          # the last complete month
    p, attrs = fc.panel(stock, sales, rules, last)
    if p.empty:
        return {"branch": pd.DataFrame(), "company": pd.DataFrame(), "accuracy": pd.DataFrame()}
    f = fc.forecast_at(p, attrs, last, rules, fc.season_tables(sales, rules, until=last.end_time))

    horizon = max(1, round((rules["replacement_days"] + rules["review_days"]) / 30.4375))
    n = int(rules["fc_backtest_months"])
    origins = [last - horizon - i for i in range(n - horizon, -1, -1)]
    test = fc.backtest(p, attrs, sales, rules, origins, horizon)
    acc = []
    for level, by in ACCURACY_LEVELS.items():
        a = fc.accuracy(test, by) if len(test) else pd.DataFrame()
        if len(a):
            a["key"] = a[by].astype(str).agg(" @ ".join, axis=1) if by else ""
            acc.append(a.assign(level=level, horizon_months=horizon)[
                ["level", "key", "horizon_months", "origins", "actual", "forecast", "wape", "wape_last_year",
                 "wape_avg12", "bias", "better"]])
    accuracy = pd.concat(acc, ignore_index=True) if acc else pd.DataFrame()

    now = stock[stock["snapshot_date"] == as_of]
    current = now.groupby(["brick_group", "branch"])["pcs"].sum()
    demand = sales[~sales["order_booked"].fillna(False).astype(bool)] if rules["fc_exclude_orders"] else sales
    year = demand[(demand["pcs"] > 0) & (demand["sale_date"] > as_of - pd.DateOffset(months=12))]
    value = year.groupby("brick_group")["value"].sum()
    months = int(rules["display_min_months"])
    recent = demand[(demand["pcs"] > 0) & (demand["sale_date"] > as_of - pd.DateOffset(months=months))]
    sold_recently = set(zip(recent["brick_group"], recent["branch"]))
    rows = ist.compute(f, current, value, sold_recently, rules, q.stock_params(conn), q.ideal_overrides(conn),
                       fc.dispersion(test) if len(test) else ({}, 1.5))
    # attributes also for groups held but never forecast (stock with no sales history)
    info = now.drop_duplicates(["brick_group", "branch"]).set_index(["brick_group", "branch"])
    for c in ("item_class_group", "purity", "item_name"):
        miss = rows[c].isna()
        if miss.any():
            look = pd.MultiIndex.from_frame(rows.loc[miss, ["brick_group", "branch"]])
            rows.loc[miss, c] = info[c].reindex(look).to_numpy()
    return {"branch": rows, "company": ist.company(rows, rules), "accuracy": accuracy}


def save_ideal_stock(conn, result, as_of, run_id):
    """Replace this stock date's ideal stock and accuracy rows."""
    for table in ("ideal_stock", "forecast_accuracy"):
        conn.execute(text(f"DELETE FROM brain.{table} WHERE as_of = :a"), {"a": as_of})
    cols = [r[0] for r in conn.execute(text("""SELECT column_name FROM information_schema.columns
                                                WHERE table_schema = 'brain' AND table_name = 'ideal_stock'"""))]
    parts = [result["branch"].rename(columns={"branch": "scope"}), result["company"].assign(scope="Company")]
    df = pd.concat([x for x in parts if len(x)], ignore_index=True)
    if len(df):
        df = df.assign(as_of=as_of, run_id=run_id).replace([np.inf, -np.inf], np.nan)
        df[[c for c in cols if c in df]].to_sql("ideal_stock", conn, schema="brain", if_exists="append", index=False,
                                                 method="multi", chunksize=1000)
    acc = result["accuracy"]
    if len(acc):
        acc.assign(as_of=as_of, run_id=run_id).replace([np.inf, -np.inf], np.nan).to_sql(
            "forecast_accuracy", conn, schema="brain", if_exists="append", index=False, method="multi", chunksize=1000)


def festival_outlook(conn, rules, as_of, stock):
    """Per upcoming festival (within the lead time) and product: expected festival sales vs pieces held.

    Expected = the product's average pieces in that festival's window over past years × its growth
    (last 12 months' pieces ÷ its average year in the history, kept within 0.5-2×).
    """
    daily = q.hist_daily(conn)
    fests = q.festivals(conn)
    if daily.empty or fests.empty:
        return pd.DataFrame()
    as_of = pd.Timestamp(as_of)
    lift = sn.festival_lift(daily, fests, "item_name", rules["festival_days_before"], rules["festival_days_after"],
                            rules["season_min_year_pcs"])
    year = sn.monthly_index(daily, "item_name", rules["season_min_year_pcs"]).set_index("item_name")["avg_year_pcs"]
    recent = q.brick_sales(conn, (as_of - timedelta(days=365)).date())
    recent = recent[recent["sale_date"] <= as_of].groupby("item_name")["pcs"].sum()
    growth = (recent / year).clip(0.5, 2.0)
    held = stock.groupby("item_name").agg(held_pcs=("pcs", "sum"), held_value=("value", "sum"),
                                          group=("item_class_group", "first"))
    ahead = fests[(fests["day"] > as_of) & (fests["day"] <= as_of + pd.Timedelta(days=rules["festival_lead_days"]))]
    rows = []
    for f in ahead.itertuples():
        for r in lift[lift["festival"] == f.name].itertuples():
            if r.item_name not in held.index:
                continue
            g = float(growth.get(r.item_name, 1.0)) if pd.notna(growth.get(r.item_name, np.nan)) else 1.0
            rows.append({"festival": f.name, "day": f.day.date(), "days_away": (f.day - as_of).days,
                         "item_name": r.item_name, "group": held.at[r.item_name, "group"], "lift": r.lift,
                         "years": r.years, "past_window_pcs": r.window_pcs, "growth": g,
                         "expected_pcs": r.window_pcs * g, "held_pcs": held.at[r.item_name, "held_pcs"],
                         "held_value": held.at[r.item_name, "held_value"]})
    return pd.DataFrame(rows)


def festival_candidates(conn, rules, as_of, stock):
    outlook = festival_outlook(conn, rules, as_of, stock)
    return gen.festival_stock(outlook, rules) if len(outlook) else []


def broken_sets(conn, stock):
    """Sets in the latest stock that cannot be sold whole (kkj.metrics.sets.broken_sets)."""
    ids = stock["set_item_barcode"].map(sets.set_id).dropna().unique()
    if not len(ids):
        return sets.broken_sets(stock.iloc[:0], stock.iloc[:0])
    history, sold = q.set_history(conn, ids)
    return sets.broken_sets(stock, history, sold)


def save_brick_turns(conn, table, as_of, run_id):
    conn.execute(text("DELETE FROM brain.brick_turn WHERE as_of = :a"), {"a": as_of})
    cols = [r[0] for r in conn.execute(text("""SELECT column_name FROM information_schema.columns
                                                WHERE table_schema = 'brain' AND table_name = 'brick_turn'"""))]
    df = table.assign(as_of=as_of, run_id=run_id)
    df[[c for c in cols if c in df]].to_sql("brick_turn", conn, schema="brain", if_exists="append", index=False,
                                             method="multi", chunksize=1000)


def candidates(conn, rules):
    """Every generator over the current data; returns (candidates, stock_as_of, sales_to, brick turn table,
    demand forecast and ideal stock)."""
    stock = q.stock_latest(conn)
    if stock.empty:
        return [], None, None, None, None
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
    existing = {k for (k,) in conn.execute(text("""SELECT action_key FROM brain.action
                                                   WHERE kind = 'offer' AND status IN ('open', 'approved', 'snoozed')"""))}
    actions += gen.hold_markdowns(gen.offers(stock, sales, rules, showrooms, exclude_barcodes=moving),
                                  q.festivals(conn), as_of, rules, existing)
    actions += gen.liquidation(stock, rules)
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
    ideal = demand_and_ideal(conn, rules, as_of)
    if len(ideal["company"]):            # increase / reduce follow the ideal stock
        actions += gen.ideal_stock(ideal, turns, rules)
    else:                                # no forecast possible (no history): the turn method as a fallback
        actions += gen.stock_turn(turns, rules)
    actions += festival_candidates(conn, rules, as_of, stock)
    actions += gen.broken_sets(broken_sets(conn, stock), rules)
    return actions, as_of.date(), sales["sale_date"].max().date() if len(sales) else None, turns, ideal


def refresh(trigger="manual"):
    """One full refresh. Returns the run's counts."""
    engine = settings.app_engine()
    with engine.begin() as conn:
        run_id = conn.execute(text("INSERT INTO brain.run_log (trigger) VALUES (:t) RETURNING run_id"),
                              {"t": trigger}).scalar_one()
    try:
        with engine.begin() as conn:
            rules = rules_mod.load(conn)
            cands, as_of, sales_to, turns, ideal = candidates(conn, rules)
            if turns is not None:
                save_brick_turns(conn, turns, as_of, run_id)
            if ideal is not None:
                save_ideal_stock(conn, ideal, as_of, run_id)
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
