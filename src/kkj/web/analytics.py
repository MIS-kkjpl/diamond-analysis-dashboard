"""Analytics pages: Inventory health, Sales, Insights, Stock turn. Filters are plain GET parameters (shareable links)."""
import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from kkj import rules as rules_mod
from kkj.db import queries as q
from kkj.metrics import brick_turn as bt
from kkj.metrics import core as m
from kkj.metrics import insights as ins
from kkj.metrics import sets
from kkj.metrics.summary import fy_start
from kkj.viz import GROUP_ORDER, money
from kkj.web import charts
from kkj.web.common import cached, engine, read, render

router = APIRouter()


def _rules():
    with engine().connect() as conn:
        return rules_mod.load(conn)


def _short(b):
    return str(b).split(" / ")[0] if isinstance(b, str) else "–"


# ── Inventory health ──────────────────────────────────────────────────────────
@router.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request, branch: list[str] = Query(default=[]), group: list[str] = Query(default=[]),
              old_days: int = 365):
    branch, group = [b for b in branch if b], [g for g in group if g]
    stock = m.company_stock(cached(q.stock_latest)).copy()
    all_branches = sorted(stock["branch"].dropna().unique())
    all_groups = sorted(stock["item_class_group"].dropna().unique())
    if branch:
        stock = stock[stock["branch"].isin(branch)]
    if group:
        stock = stock[stock["item_class_group"].isin(group)]
    ctx = {"f": {"branch": branch, "group": group, "old_days": old_days}, "branches": all_branches, "groups": all_groups}
    if stock.empty:
        return render(request, "inventory.html", "inventory", empty=True, **ctx)
    stock["value"] = m.value(stock)
    stock["age_band"] = m.age_bucket(stock["age_days"]).values
    stock["where"] = m.location_type(stock["location"])
    total = stock["value"].sum()
    old = stock[stock["age_days"] > old_days]
    approval, transit = stock[stock["where"] == "Approval"], stock[stock["where"] == "In transit"]
    kpis = [("Stock value", money(total), f"{stock['pcs'].sum():,.0f} pcs", "ti-diamond", "blue"),
            (f"Older than {old_days} days", f"{100 * old['value'].sum() / total:.1f}%", money(old["value"].sum()), "ti-hourglass", "orange"),
            ("Median age", f"{stock['age_days'].median():,.0f} days", "since labelling", "ti-clock", "azure"),
            ("On approval", money(approval["value"].sum()), f"{len(approval):,} labels", "ti-user-share", "purple"),
            ("In transit", money(transit["value"].sum()), f"{len(transit):,} labels", "ti-truck", "cyan")]

    by_branch = stock.groupby("branch", as_index=False).agg(value=("value", "sum"), labels=("barcode", "size"),
                                                            pcs=("pcs", "sum")).sort_values("value", ascending=False)
    by_age = stock.groupby("age_band", observed=False, as_index=False).agg(value=("value", "sum"), labels=("barcode", "size"))
    by_age["share"] = by_age["value"] / total
    pivot = stock.pivot_table(index="item_class_group", columns="age_band", values="value", aggfunc="sum",
                              observed=False, fill_value=0)
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("Total", ascending=False).reset_index()
    oldest = old.sort_values("value", ascending=False).head(200).assign(branch=lambda d: d["branch"].map(_short))
    away = stock[stock["where"].isin(["Approval", "In transit", "Repair"])]
    away_t = away.groupby(["where", "location"], as_index=False).agg(
        labels=("barcode", "size"), value=("value", "sum"), longest_days_in_branch=("days_from_branch_in", "max")
    ).sort_values("value", ascending=False)

    t_sets, t_broken = _sets_tables(stock)
    return render(
        request, "inventory.html", "inventory", empty=False, kpis=kpis, as_of=stock["snapshot_date"].iloc[0], **ctx,
        t_sets=t_sets, t_broken=t_broken,
        fig_branch=charts.hbar(by_branch["branch"].map(_short), by_branch["value"] / 1e7, by_branch["labels"],
                               " · %{customdata:,} labels"),
        t_branch=charts.table(by_branch, {"value": "money", "labels": "num", "pcs": "num"}),
        fig_age=charts.ordinal_bars(by_age["age_band"], by_age["value"] / 1e7, " Cr", "₹",
                                    np.c_[by_age["labels"], 100 * by_age["share"]],
                                    "<br>%{customdata[0]:,} labels · %{customdata[1]:.1f}%"),
        t_age=charts.table(by_age.assign(age_band=by_age["age_band"].astype(str)), {"value": "money", "share": "pct"}),
        t_pivot=charts.table(pivot.rename(columns=str), {c: "money" for c in pivot.columns if c != "item_class_group"},
                             {"item_class_group": "item group"}),
        t_oldest=charts.table(oldest[["barcode", "item_name", "category", "item_class_group", "branch", "location",
                                      "supplier", "age_days", "days_from_branch_in", "dia_wt", "value"]],
                              {"value": "money", "age_days": "num", "days_from_branch_in": "num", "dia_wt": "dec"}),
        t_away=charts.table(away_t, {"value": "money", "labels": "num", "longest_days_in_branch": "num"}))


def _sets_tables(stock):
    """Sets in stock by set type, and the broken ones (kkj.metrics.sets)."""
    units = sets.stock_units(stock)
    multi = units[units["members"] > 1]
    by_type = multi.groupby("item_name", as_index=False).agg(
        sets=("pcs", "size"), labels=("members", "sum"), value=("value", "sum"), avg_carats=("dia_wt", "mean")
    ).sort_values("value", ascending=False).rename(columns={"item_name": "set type"})
    t_sets = charts.table(by_type, {"sets": "num", "labels": "num", "value": "money", "avg_carats": "dec"})
    ids = tuple(sorted(stock["set_item_barcode"].map(sets.set_id).dropna().unique()))
    if not ids:
        return t_sets, None
    history, sold = cached(q.set_history, ids)
    broken = sets.broken_sets(stock, history, sold)
    if broken.empty:
        return t_sets, None
    broken = broken.assign(where=broken["where"].map(lambda w: "; ".join(_short(p.split(" · ")[0]) + " · " +
                                                                       p.split(" · ")[-1] for p in w.split(sets.PLACE_SEP))))
    t_broken = charts.table(broken[["set_id", "set_type", "problem", "left_in_stock", "where", "missing", "age_days",
                                    "value_left"]],
                            {"value_left": "money", "age_days": "num"}, {"set_id": "set", "value_left": "value left"})
    return t_sets, t_broken


# ── Sales ─────────────────────────────────────────────────────────────────────
@router.get("/sales", response_class=HTMLResponse)
def sales(request: Request, start: str = "", end: str = "", branch: list[str] = Query(default=[]),
          group: list[str] = Query(default=[]), salesperson: str = ""):
    branch, group = [b for b in branch if b], [g for g in group if g]
    today = pd.Timestamp.today().normalize()
    start_d = pd.Timestamp(start) if start else fy_start(today)
    end_d = pd.Timestamp(end) if end else today
    lines = m.net_sales(cached(q.sales_lines, start_d.date()))
    lines = lines[(lines["sale_date"] >= start_d) & (lines["sale_date"] <= end_d)]
    all_branches = sorted(lines["branch"].dropna().unique())
    all_groups = [g for g in GROUP_ORDER if g in set(lines["item_class_group"])]
    if branch:
        lines = lines[lines["branch"].isin(branch)]
    if group:
        lines = lines[lines["item_class_group"].isin(group)]
    from kkj.salespeople import split_salesman
    codes = lines["salesman"].fillna("").map(lambda v: split_salesman(v)[0] if v else "")
    with engine().connect() as conn:
        named = q.salespeople(conn).set_index("sales_code")["name"]
    seller_options = sorted({(c, named.get(c, c)) for c in codes.unique() if c}, key=lambda x: x[1])
    if salesperson:
        lines = lines[codes == salesperson]
    ctx = {"f": {"start": start_d.date().isoformat(), "end": end_d.date().isoformat(), "branch": branch, "group": group,
                 "salesperson": salesperson},
           "branches": all_branches, "groups": all_groups, "sellers": seller_options}
    if lines.empty:
        return render(request, "sales.html", "sales", empty=True, **ctx)
    sold, back = lines[lines["pcs"] > 0], lines[lines["pcs"] < 0]
    gross, returned = sold["net_value"].sum(), -back["net_value"].sum()
    kpis = [("Net sales", money(lines["net_value"].sum()), f"{lines['net_pcs'].sum():,.0f} pcs net", "ti-receipt", "green"),
            ("Returns", money(returned), f"{100 * returned / gross:.1f}% of gross · {len(back)} lines" if gross else "",
             "ti-arrow-back-up", "red"),
            ("Average sale value", money(gross / sold["pcs"].sum()) if sold["pcs"].sum() else "–", "per piece", "ti-currency-rupee", "blue"),
            ("Median days to sell", f"{sold['age_days_at_sale'].median():,.0f} days", "label date → sale", "ti-clock", "azure"),
            ("Bills", f"{sold['bill_no'].nunique():,}", "", "ti-file-invoice", "purple")]
    monthly = (lines.assign(month=lines["sale_date"].dt.to_period("M").dt.to_timestamp())
               .groupby(["month", "item_class_group"], as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum")))
    by_branch = lines.groupby("branch", as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum")) \
        .sort_values("net", ascending=False)
    by_item = lines.groupby("item_name", as_index=False).agg(net=("net_value", "sum"), pcs=("net_pcs", "sum")) \
        .sort_values("net", ascending=False)
    sold_b = sold.assign(age_band=m.age_bucket(sold["age_days_at_sale"]).values)
    by_age = sold_b.groupby("age_band", observed=False, as_index=False).agg(pcs=("pcs", "sum"), value=("net_value", "sum"))

    with engine().connect() as conn:
        people = q.salespeople(conn)
    from kkj.salespeople import split_salesman
    staff = lines.dropna(subset=["salesman"]).assign(code=lambda d: d["salesman"].map(lambda v: split_salesman(v)[0]))
    staff = staff.merge(people.rename(columns={"sales_code": "code", "name": "person", "branch": "home_branch"}),
                        on="code", how="left")
    staff["kind"] = staff["kind"].fillna("salesperson")
    staff = staff[staff["kind"] == "salesperson"]
    staff["discount"] = (staff["item_amt"] - staff["taxable_amount"]).where(staff["taxable_amount"].notna() & (staff["pcs"] > 0))
    team = staff.groupby(staff["person"].fillna(staff["salesman"])).agg(
        net_sales=("net_value", "sum"), pcs=("net_pcs", "sum"), bills=("bill_no", "nunique"),
        discount=("discount", "sum"), label_value=("item_amt", lambda x: x[staff.loc[x.index, "pcs"] > 0].sum()),
        returns=("pcs", lambda x: int((x < 0).sum()))).reset_index(names="salesperson")
    team["discount_rate"] = team["discount"] / team["label_value"].replace(0, np.nan)
    team = team.drop(columns=["discount", "label_value"]).sort_values("net_sales", ascending=False)

    return render(
        request, "sales.html", "sales", empty=False, kpis=kpis, legacy=(lines["source"] == "csv_legacy").any(), **ctx,
        fig_monthly=charts.monthly_by_group(monthly),
        t_monthly=charts.table(monthly.pivot_table(index="month", columns="item_class_group", values="net", aggfunc="sum",
                                                   fill_value=0).reset_index().assign(month=lambda d: d["month"].dt.strftime("%b %Y")),
                               {g: "money" for g in all_groups}),
        fig_branch=charts.hbar(by_branch["branch"].map(_short), by_branch["net"] / 1e7, by_branch["pcs"], " · %{customdata:,.0f} pcs"),
        t_branch=charts.table(by_branch, {"net": "money", "pcs": "num"}, {"net": "net sales"}),
        fig_items=charts.hbar(by_item["item_name"].head(10), by_item["net"].head(10) / 1e7, by_item["pcs"].head(10),
                              " · %{customdata:,.0f} pcs"),
        t_items=charts.table(by_item, {"net": "money", "pcs": "num"}, {"net": "net sales"}),
        fig_age=charts.ordinal_bars(by_age["age_band"], by_age["pcs"], " pcs", "", by_age["value"] / 1e7,
                                    "<br>₹%{customdata:.2f} Cr"),
        t_sets=charts.table(sets.sold_whole_or_split(lines), {"sets_sold": "num", "sold_whole": "num", "split": "num",
                                                                 "whole_share": "pct"},
                            {"whole_share": "sold whole", "sold_whole": "whole (same day)", "split": "split (different days)"}),
        t_team=charts.table(team, {"net_sales": "money", "pcs": "num", "bills": "num", "returns": "num",
                                   "discount_rate": "pct"}, {"discount_rate": "discount given"}),
        t_returns=charts.table(back.sort_values("sale_date", ascending=False)[
            ["sale_date", "bill_no", "orig_bill_no", "orig_sale_date", "item_name", "branch", "salesman", "pcs", "net_value"]]
            .assign(branch=lambda d: d["branch"].map(_short)),
            {"sale_date": "date", "orig_sale_date": "date", "net_value": "money", "pcs": "num"},
            {"sale_date": "returned on", "orig_bill_no": "sold on bill", "orig_sale_date": "sold on", "net_value": "value"}))


# ── Insights ──────────────────────────────────────────────────────────────────
@router.get("/insights", response_class=HTMLResponse)
def insights(request: Request, group: list[str] = Query(default=["Diamond Jewellery"])):
    rules = _rules()
    latest = m.company_stock(cached(q.stock_latest))
    as_of = pd.Timestamp(latest["snapshot_date"].iloc[0])
    history = ins.one_per_week(cached(q.stock_history, (as_of - pd.DateOffset(months=rules["insight_months"])).date()))
    all_groups = sorted(history["item_class_group"].dropna().unique())
    group = [g for g in group if g]
    if group:
        history = history[history["item_class_group"].isin(group)]
        latest = latest[latest["item_class_group"].isin(group)]
    start, end = history["snapshot_date"].min(), history["snapshot_date"].max()
    lines = cached(q.sales_lines, start.date())
    s = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= end) & (lines["pcs"] > 0)].copy()
    if group:
        s = s[s["item_class_group"].isin(group)]
    s["value"], s["age_at_sale"] = m.value(s), s["age_days_at_sale"]
    s["discount"] = 1 - s["taxable_amount"] / s["item_amt"]

    latest = latest.assign(value=m.value(latest))
    aged = latest[latest["age_days"] > rules["aged_days"]]
    dead = latest[latest["age_days"] > rules["liquidation_age_days"]]
    recovery = ins.recovery_value(dead, rules["diamond_recovery_pct"] / 100)
    by_age = ins.sell_rate_by_age(history, s, start, end)
    suggestion = ins.suggested_aged_days(by_age)
    kpis = [(f"Aged stock (over {rules['aged_days']} days)", money(aged["value"].sum()),
             f"{100 * aged['value'].sum() / latest['value'].sum():.1f}% of stock" if len(latest) else "", "ti-hourglass", "orange"),
            ("Cost of holding it", money(ins.carrying_cost_per_month(aged["value"].sum(), rules["capital_cost_pct"])) + " / month",
             f"at {rules['capital_cost_pct']:g}% a year", "ti-coin-rupee", "red"),
            (f"Clearance candidates (over {rules['liquidation_age_days']} days)", money(dead["value"].sum()),
             f"{len(dead):,} pieces · recovery ≈ {money(recovery.sum())}", "ti-recycle", "purple"),
            ("Data suggests 'aged' from", f"{suggestion} days" if suggestion else "–",
             f"setting now {rules['aged_days']} days", "ti-adjustments", "blue")]

    # 1. age
    t1 = by_age.reset_index(names="age")
    new_rate = by_age["monthly_sell_rate"].iloc[:2].mean()
    oldest = by_age.iloc[-1]
    act1 = (f"A new piece sells about {new_rate / by_age['monthly_sell_rate'].iloc[3]:.1f}× as fast as a 6–12-month-old one. "
            f"Act before pieces reach the slow end: at {oldest.name} only {100 * oldest['monthly_sell_rate']:.1f}% sell a month, "
            f"so the {money(oldest['avg_value'])} held there needs a clearance plan, not a markdown.")
    # 2. suppliers
    bands = [0] + list(rules["price_band_edges"]) + [np.inf]
    sup = ins.supplier_scorecard(history, s, start, end, bands, rules["min_supplier_pcs"]).dropna(subset=["performance"])
    slow, fast = sup[sup["performance"] < 0.5], sup[sup["performance"] > 1.5]
    act2 = (f"Increase purchases from the {len(fast)} suppliers selling over 1.5× the company rate at their price points "
            f"({', '.join(fast.sort_values('performance', ascending=False).index[:5]) or '–'}); review the {len(slow)} under 0.5× "
            f"(holding {money(slow['avg_value'].sum())}) for exchanges or returns.")
    # 3. assortment
    edges = [-np.inf] + list(rules["price_band_edges"]) + [np.inf]
    from kkj.brain.generators import band_money
    names = [f"< {band_money(edges[1])}"] + [f"{band_money(a)}–{band_money(b)}" for a, b in zip(edges[1:-2], edges[2:-1])] \
        + [f"{band_money(edges[-2])} +"]
    gap = ins.assortment_gap(history.assign(band=pd.cut(history["value"], edges, labels=names)),
                             s.assign(band=pd.cut(s["value"], edges, labels=names)), start, end, "band").reset_index(names="band")
    items = ins.assortment_gap(history, s, start, end, "item_name")
    items = items[items["avg_pcs"] >= 20].sort_values("months_of_stock").reset_index(names="item")
    under = gap[gap["gap"] > 0.02]["band"].astype(str).tolist()
    over = gap[gap["gap"] < -0.02]["band"].astype(str).tolist()
    act3 = (f"Shift purchasing towards {', '.join(under) or '–'} (a bigger share of sales than of stock) and away from "
            f"{', '.join(over) or '–'}. Items at the bottom of the table hold the most months of stock.")
    # 4. channels
    with engine().connect() as conn:
        kinds = q.branches(conn).set_index("erp_branch")["kind"]
    channel = s["branch"].map(kinds).fillna("other").map(
        lambda k: "Exhibition" if k == "exhibition" else "Showroom" if k == "showroom" else "Other")
    ch = ins.channel_comparison(s, channel).reset_index()
    # 5. clearance
    liq = dead.assign(recovery=recovery.values, age_years=dead["age_days"] / 365, branch=dead["branch"].map(_short))
    liq["recovery_share"] = liq["recovery"] / liq["value"]

    return render(
        request, "insights.html", "insights", kpis=kpis, groups=all_groups, f={"group": group},
        period=f"{start:%d %b %Y} to {end:%d %b %Y} · {history['snapshot_date'].nunique()} stock snapshots · {len(s):,} sale lines",
        fig_age=charts.ordinal_bars(t1["age"], 100 * t1["monthly_sell_rate"], "%", "",
                                    np.c_[t1["avg_pcs"], t1["avg_value"] / 1e7, t1["months_to_sell_half"]],
                                    " sold a month<br>%{customdata[0]:,.0f} pcs held · ₹%{customdata[1]:.1f} Cr"
                                    "<br>half sell in %{customdata[2]:.0f} months"),
        t_age=charts.table(t1[["age", "avg_pcs", "avg_value", "sold_per_month", "monthly_sell_rate", "months_to_sell_half"]],
                           {"avg_pcs": "num", "avg_value": "money", "sold_per_month": "dec", "monthly_sell_rate": "pct",
                            "months_to_sell_half": "dec"},
                           {"avg_pcs": "held (pcs)", "avg_value": "held", "monthly_sell_rate": "sold a month",
                            "months_to_sell_half": "months for half to sell"}),
        act1=act1,
        fig_sup=charts.performance_bars(sup.index.tolist(), sup["performance"],
                                        np.c_[100 * sup["monthly_sell_rate"], sup["avg_value"] / 1e7],
                                        "<br>%{customdata[0]:.1f}% sold a month · ₹%{customdata[1]:.2f} Cr held") if len(sup) else None,
        t_sup=charts.table(sup.reset_index(names="supplier")[["supplier", "performance", "monthly_sell_rate", "avg_value",
                                                              "avg_pcs", "annual_turn", "median_days_to_sell", "aged_share_now"]],
                           {"performance": "dec", "monthly_sell_rate": "pct", "avg_value": "money", "avg_pcs": "num",
                            "annual_turn": "dec", "median_days_to_sell": "num", "aged_share_now": "pct"},
                           {"performance": "performance (×)", "monthly_sell_rate": "sold a month", "avg_value": "held",
                            "avg_pcs": "held (pcs)", "annual_turn": "turn (×/yr)", "aged_share_now": "aged now"}),
        act2=act2,
        fig_band=charts.two_series(gap["band"].astype(str).tolist(), 100 * gap["share_of_stock"], 100 * gap["share_of_sales"],
                                   "Share of stock (pcs)", "Share of sales (pcs)"),
        t_items=charts.table(items[["item", "avg_pcs", "sold_per_month", "months_of_stock", "share_of_stock", "share_of_sales"]],
                             {"avg_pcs": "num", "sold_per_month": "dec", "months_of_stock": "dec", "share_of_stock": "pct",
                              "share_of_sales": "pct"}, {"avg_pcs": "held (pcs)"}),
        act3=act3,
        t_channel=charts.table(ch, {"lines": "num", "value": "money", "median_age_at_sale": "num", "share_over_1y": "pct",
                                    "median_discount": "pct"},
                               {"median_age_at_sale": "median age at sale (days)", "share_over_1y": "sold pieces over 1 yr old"}),
        t_liq=charts.table(liq.sort_values("value", ascending=False)[
            ["barcode", "item_name", "category", "branch", "location", "supplier", "age_years", "net_wt", "dia_wt", "value",
             "recovery", "recovery_share"]],
            {"age_years": "dec", "net_wt": "dec", "dia_wt": "dec", "value": "money", "recovery": "money", "recovery_share": "pct"},
            {"value": "tag value", "recovery": "recovery value", "recovery_share": "recovery %"}),
        recovery_pct=rules["diamond_recovery_pct"], liq_days=rules["liquidation_age_days"])


# ── Stock turn (weighted, per brick) ──────────────────────────────────────────
@router.get("/stock-turn", response_class=HTMLResponse)
def stock_turn(request: Request, view: str = "group", scope: str = "Company", group: list[str] = Query(default=[]),
               status: list[str] = Query(default=[]), search: str = ""):
    rules = _rules()
    t = read("""SELECT * FROM brain.brick_turn WHERE as_of = (SELECT max(as_of) FROM brain.brick_turn)""")
    if t.empty:
        return render(request, "stock_turn.html", "stock_turn", empty=True, f={}, rules=rules)
    as_of = t["as_of"].iloc[0]
    view = view if view in ("group", "brick") else "group"
    t = t[t["level"] == view]
    scopes = ["Company"] + sorted(s for s in t["scope"].unique() if s != "Company")
    scope = scope if scope in scopes else "Company"
    t = t[t["scope"] == scope]
    groups = sorted(t["item_class_group"].dropna().unique())
    group, status = [g for g in group if g], [s for s in status if s in bt.STATUS]
    if group:
        t = t[t["item_class_group"].isin(group)]
    if search:
        t = t[t["brick"].str.contains(search, case=False, regex=False)]
    t = t.assign(apv=t["avg_piece_value"].astype(float).fillna(0))
    t["change_value"] = t["change_pcs"].astype(float) * t["apv"]

    by_status = t.groupby("status").agg(bricks=("brick", "size"), pcs=("current_pcs", "sum"), value=("current_value", "sum"),
                                        change=("change_value", "sum")).reindex(list(bt.STATUS)).dropna(how="all")
    def k(s, icon, colour, sub):
        r = by_status.loc[s] if s in by_status.index else None
        return (bt.STATUS[s][0], f"{int(r['bricks']) if r is not None else 0:,} bricks", sub(r) if r is not None else "",
                icon, colour)
    kpis = [k("increase", "ti-trending-up", "green", lambda r: f"{money(r['change'])} more stock needed"),
            k("reduce", "ti-trending-down", "orange", lambda r: f"{money(-r['change'])} more than sales need"),
            k("keep", "ti-check", "blue", lambda r: f"{money(r['value'])} held"),
            k("phase_out", "ti-trash", "red", lambda r: f"{money(r['value'])} held"),
            ("Too new / hold / thin", f"{int(by_status.reindex(['new', 'hold', 'stock_up', 'no_history'])['bricks'].sum()):,} bricks",
             f"{money(by_status.reindex(['new', 'hold', 'stock_up', 'no_history'])['value'].sum())} held", "ti-eye", "secondary")]

    chart = by_status.reset_index()
    chart["label"] = chart["status"].map(lambda s: bt.STATUS[s][0])
    shown = t[t["status"].isin(status)] if status else t
    shown = shown.assign(order=shown["change_value"].abs().where(shown["status"] != "phase_out", shown["current_value"]))
    shown = shown.sort_values("order", ascending=False).head(300)
    def turns(r):
        return " · ".join("–" if pd.isna(r[c]) else ("∞" if np.isinf(r[c]) else f"{r[c]:.2f}") for c in ("turn_3m", "turn_6m", "turn_12m"))
    set_for = {"default": "default", "item_group": "item group", "product": "product", "group": "brick group", "brick": "brick"}
    shown = shown.assign(
        status_label=shown["status"].map(lambda s: bt.STATUS[s][0]),
        windows=shown.apply(turns, axis=1),
        ideal=shown.apply(lambda r: f"{r['ideal_turn']:g}× ({set_for.get(r['ideal_source'], 'default')})", axis=1))
    table = charts.table(
        shown[["brick", "current_pcs", "ideal_pcs", "change_pcs", "change_value", "blended_turn", "ideal", "windows",
               "status_label"]],
        {"current_pcs": "num", "ideal_pcs": "num", "change_pcs": "num", "change_value": "money", "blended_turn": "dec"},
        {"brick": "brick group" if view == "group" else "brick", "current_pcs": "now (pcs)", "ideal_pcs": "ideal (pcs)",
         "change_pcs": "change (pcs)", "change_value": "change (₹)", "blended_turn": "turn (× a year)",
         "ideal": "ideal turn", "windows": "turn 3 · 6 · 12 months", "status_label": "verdict"})
    return render(request, "stock_turn.html", "stock_turn", empty=False, rules=rules, as_of=as_of, kpis=kpis,
                  scopes=scopes, groups=groups, statuses=bt.STATUS, f={"view": view, "scope": scope, "group": group, "status": status, "search": search},
                  fig=charts.hbar(chart["label"], chart["value"] / 1e7, np.c_[chart["bricks"], chart["pcs"]],
                                  "<br>%{customdata[0]:,.0f} bricks · %{customdata[1]:,.0f} pcs"),
                  t=table, n_shown=len(shown), n_total=len(t[t["status"].isin(status)] if status else t),
                  levels=" – ".join(bt.LEVEL_LABELS[lv] for lv in bt.LEVELS
                                    if lv in rules["turn_brick_levels" if view == "brick" else "turn_group_levels"]),
                  n_ideals=int(read("SELECT count(*) AS n FROM config.ideal_turn")["n"].iloc[0]))


# ── Seasonality ───────────────────────────────────────────────────────────────
MONTH_NAMES = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


@router.get("/seasonality", response_class=HTMLResponse)
def seasonality(request: Request, group: str = "Diamond Jewellery"):
    from kkj.brain.run import festival_outlook
    from kkj.metrics import seasonality as sn
    rules = _rules()
    daily = cached(q.hist_daily)
    if daily.empty:
        return render(request, "seasonality.html", "seasonality", empty=True, rules=rules)
    fests = cached(q.festivals)
    by_group = sn.monthly_index(daily, "item_class_group", rules["season_min_year_pcs"])
    by_group = by_group.sort_values("avg_year_pcs", ascending=False)
    # the history is diamond groups only (queries.hist_daily); in the app's usual order
    groups = [g for g in GROUP_ORDER if g in set(by_group["item_class_group"])] +         [g for g in by_group["item_class_group"] if g not in GROUP_ORDER]
    group = group if group in groups else groups[0]
    cols = [f"index_{m}" for m in sn.MONTHS]
    shown_groups = groups[:4]
    top = by_group.set_index("item_class_group").loc[shown_groups].reset_index()
    fig_groups = charts.season_lines(MONTH_NAMES, {r["item_class_group"]: [r[c] for c in cols]
                                                   for r in top.to_dict("records")})
    t_groups = charts.table(by_group[["item_class_group", "avg_year_pcs", "years"] + cols].rename(
        columns=dict(zip(cols, MONTH_NAMES))), {**{m: "dec" for m in MONTH_NAMES}, "avg_year_pcs": "num"},
        {"item_class_group": "item group", "avg_year_pcs": "pieces a year"})

    in_group = daily[daily["item_class_group"] == group]
    by_product = sn.monthly_index(in_group, "item_name", rules["season_min_year_pcs"]).sort_values("avg_year_pcs",
                                                                                                    ascending=False)
    t_products = charts.table(by_product[["item_name", "avg_year_pcs", "years"] + cols].rename(
        columns=dict(zip(cols, MONTH_NAMES))), {**{m: "dec" for m in MONTH_NAMES}, "avg_year_pcs": "num"},
        {"item_name": "product", "avg_year_pcs": "pieces a year"})

    lift = sn.festival_lift(daily, fests, "item_class_group", rules["festival_days_before"],
                            rules["festival_days_after"], rules["season_min_year_pcs"])
    lift = lift[lift["item_class_group"].isin(groups[:6])]
    lift = lift.assign(order=lift["item_class_group"].map({g: i for i, g in enumerate(groups)})).sort_values("order")
    t_lift = charts.table(lift.pivot_table(index="festival", columns="item_class_group", values="lift")
                          .reindex(columns=[g for g in groups if g in set(lift["item_class_group"])]).reset_index(),
                          {g: "dec" for g in groups}) if len(lift) else None

    today = pd.Timestamp.today().normalize()
    upcoming = fests[(fests["day"] >= today)].head(8).assign(days_away=lambda d: (d["day"] - today).dt.days)
    latest = m.company_stock(cached(q.stock_latest))
    latest = latest.assign(value=m.value(latest))
    as_of = pd.Timestamp(latest["snapshot_date"].iloc[0])
    with engine().connect() as conn:
        outlook = festival_outlook(conn, rules, as_of, latest)
    t_outlook = None
    if len(outlook):
        outlook = outlook[outlook["days_away"] >= 7]
    if len(outlook):
        outlook = outlook.assign(target=lambda d: (rules["festival_stock_cover"] * d["expected_pcs"]).round(),
                                 day=lambda d: pd.to_datetime(d["day"]).dt.strftime("%d %b %Y"))
        outlook["short"] = (outlook["target"] - outlook["held_pcs"]).clip(lower=0)
        t_outlook = charts.table(outlook.sort_values(["day", "short"], ascending=[True, False])[
            ["festival", "day", "days_away", "item_name", "years", "past_window_pcs", "growth", "expected_pcs",
             "held_pcs", "target", "short"]],
            {"past_window_pcs": "num", "growth": "dec", "expected_pcs": "num", "held_pcs": "num", "target": "num",
             "short": "num", "days_away": "num"},
            {"item_name": "product", "past_window_pcs": "past festival sales (avg pcs)", "growth": "this year ×",
             "expected_pcs": "expected", "held_pcs": "held", "target": f"target ({rules['festival_stock_cover']:g}×)",
             "days_away": "days away"})

    # the next 90 days for each diamond product: seasonal demand at this year's pace vs stock held
    idx = sn.monthly_index(daily, "item_name", rules["season_min_year_pcs"]).set_index("item_name")
    with engine().connect() as conn:
        recent = q.brick_sales(conn, (as_of - pd.Timedelta(days=365)).date())
    recent = recent[recent["sale_date"] <= as_of].groupby("item_name")["pcs"].sum()
    held = latest.groupby("item_name").agg(held=("pcs", "sum"), group=("item_class_group", "first"))
    rows = []
    for item, h in held.iterrows():
        if item not in idx.index or recent.get(item, 0) <= 0:
            continue
        share = sn.window_share(idx.loc[item], as_of + pd.Timedelta(days=1), as_of + pd.Timedelta(days=90))
        exp = recent[item] * share
        rows.append({"product": item, "group": h["group"], "sold_last_12m": recent[item], "next_90_days_share": share,
                     "expected_next_90_days": exp, "held": h["held"],
                     "months_of_cover": h["held"] / (exp / 3) if exp > 0 else np.nan})
    ahead = pd.DataFrame(rows).sort_values("expected_next_90_days", ascending=False) if rows else pd.DataFrame()
    t_ahead = charts.table(ahead, {"sold_last_12m": "num", "next_90_days_share": "pct", "expected_next_90_days": "num",
                                   "held": "num", "months_of_cover": "dec"},
                           {"next_90_days_share": "share of a year in the next 90 days"}) if len(ahead) else None

    years = sorted(set(sn.complete_years(daily)))
    period = (f"{daily['sale_date'].min():%b %Y} to {daily['sale_date'].max():%b %Y} · complete financial years used: "
              + ", ".join(f"{y}-{str(y + 1)[2:]}" for y in years) + f" · {daily['pcs'].sum():,.0f} pieces, all product groups")
    return render(request, "seasonality.html", "seasonality", empty=False, rules=rules, period=period,
                  fig_groups=fig_groups, t_groups=t_groups, groups=groups, f={"group": group}, t_products=t_products,
                  t_lift=t_lift, upcoming=upcoming.astype(object).where(upcoming.notna(), None).to_dict("records"), t_outlook=t_outlook, t_ahead=t_ahead,
                  as_of=as_of)
