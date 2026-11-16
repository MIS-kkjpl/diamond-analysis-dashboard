"""Analytics pages: Inventory health, Sales, Insights, Stock turn. Filters are plain GET parameters (shareable links)."""
from datetime import date
from html import escape as html_escape
from urllib.parse import quote, urlencode

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
from kkj.web.common import cached, engine, read, render, who

router = APIRouter()


def _rules():
    with engine().connect() as conn:
        return rules_mod.load(conn)


def _short(b):
    return str(b).split(" / ")[0] if isinstance(b, str) else "–"


# ── Inventory health ──────────────────────────────────────────────────────────
@router.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request, branch: list[str] = Query(default=[]), group: list[str] = Query(default=[]),
              old_days: int = 365, approval_days: int = 90):
    branch, group = [b for b in branch if b], [g for g in group if g]
    stock = cached(q.stock_latest).copy()
    all_branches = sorted(stock["branch"].dropna().unique())
    all_groups = sorted(stock["item_class_group"].dropna().unique())
    if branch:
        stock = stock[stock["branch"].isin(branch)]
    if group:
        stock = stock[stock["item_class_group"].isin(group)]
    ctx = {"f": {"branch": branch, "group": group, "old_days": old_days, "approval_days": approval_days},
           "branches": all_branches, "groups": all_groups}
    if stock.empty:
        return render(request, "inventory.html", "inventory", empty=True, **ctx)
    stock["value"] = m.value(stock)
    stock["age_band"] = m.age_bucket(stock["age_days"]).values
    with engine().connect() as conn:
        from kkj.scope import location_kinds
        kinds = location_kinds(conn)
    stock["where"] = m.location_type(stock["location"], kinds)
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
    # out on approval longer than approval_days: probably sold, lost or on consignment, yet still counted as stock
    stale = approval[approval["days_from_branch_in"] > approval_days].sort_values("days_from_branch_in", ascending=False)
    stale_kpi = (len(stale), stale["value"].sum())
    t_stale = charts.table(stale[["barcode", "item_name", "item_class_group", "branch", "location", "days_from_branch_in",
                                  "age_days", "value"]].assign(branch=lambda d: d["branch"].map(_short),
                                                               barcode=lambda d: [(b, f"/lookup?q={b}") for b in d["barcode"]]),
                           {"barcode": "link", "value": "money", "days_from_branch_in": "num", "age_days": "num"},
                           {"days_from_branch_in": "days on approval", "age_days": "age (days)"}) if len(stale) else None
    return render(
        request, "inventory.html", "inventory", empty=False, kpis=kpis, as_of=stock["snapshot_date"].iloc[0], **ctx,
        t_sets=t_sets, t_broken=t_broken, t_stale=t_stale, stale_kpi=stale_kpi,
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
            ("Bills", f"{sold['bill_no'].nunique():,}",
             f"{sold['pcs'].sum() / sold['bill_no'].nunique():.2f} pieces each" if sold["bill_no"].nunique() else "",
             "ti-file-invoice", "purple")]
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

    pair_h = max(260, 30 * max(len(by_branch), min(len(by_item), 10)))     # both bar charts in the row
    return render(
        request, "sales.html", "sales", empty=False, kpis=kpis, legacy=(lines["source"] == "csv_legacy").any(), **ctx,
        fig_monthly=charts.monthly_by_group(monthly),
        t_monthly=charts.table(monthly.pivot_table(index="month", columns="item_class_group", values="net", aggfunc="sum",
                                                   fill_value=0).reset_index().assign(month=lambda d: d["month"].dt.strftime("%b %Y")),
                               {g: "money" for g in all_groups}),
        fig_branch=charts.hbar(by_branch["branch"].map(_short), by_branch["net"] / 1e7, by_branch["pcs"], " · %{customdata:,.0f} pcs",
                               height=pair_h),
        t_branch=charts.table(by_branch, {"net": "money", "pcs": "num"}, {"net": "net sales"}),
        fig_items=charts.hbar(by_item["item_name"].head(10), by_item["net"].head(10) / 1e7, by_item["pcs"].head(10),
                              " · %{customdata:,.0f} pcs", height=pair_h),
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
    latest = cached(q.stock_latest)
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


# ── Ideal stock (demand forecast) ─────────────────────────────────────────────
IDEAL_STATUS = {"increase": ("Buy more", "green"), "reduce": ("Too much", "orange"), "phase_out": ("Stop stocking", "red"),
                "unproven": ("Not proven yet", "secondary"), "keep": ("About right", "secondary")}
LATEST_IDEAL = "as_of = (SELECT max(as_of) FROM brain.ideal_stock)"
REASONS = ["Wedding / festival season", "New counter or display", "Customer orders expected", "Slow seller, clear it",
           "Supplier can't deliver", "Trying a new design"]


def _num(x):
    return None if x is None or pd.isna(x) else int(round(float(x)))


def _fix_info(f):
    """A fix row as the editor shows it."""
    return {"pcs": int(f["pcs"]), "reason": f["reason"], "by": f.get("updated_by"), "at": f.get("updated_at"),
            "review_on": f.get("review_on"), "calc_at_fix": f.get("calc_at_fix"), "level": f["level"], "key": f["key"],
            "branch": f["branch"], "due": bool(f.get("review_on") and f["review_on"] <= date.today())}


def _fixes():
    fx = read("""SELECT level, key, branch, pcs, reason, updated_by, updated_at AT TIME ZONE 'Asia/Kolkata' AS updated_at,
                        review_on, calc_at_fix FROM config.ideal_stock_override ORDER BY review_on NULLS LAST""")
    return fx.astype(object).where(fx.notna(), None)


def ideal_status(held, ideal, rules):
    from kkj.metrics import ideal_stock as ist
    return str(ist.status([float(held or 0)], [float(ideal or 0)], rules)[0])


def _editor_row(key, branch, label, held, calc, ideal, status, fix=None, sub="", link=None, note=None):
    held, calc, ideal = _num(held) or 0, _num(calc), _num(ideal) or 0
    calc = ideal if calc is None else calc
    change = ideal - held
    word, colour = IDEAL_STATUS.get(status, (status, "secondary"))
    if status == "increase":
        detail = f"add {change:,}"
    elif status == "reduce":
        detail = f"{-change:,} extra"
    elif status == "phase_out":
        detail = f"clear {held:,}"
    elif status == "unproven":
        detail = "too little time on display"
    else:
        detail = ""
    return {"key": key, "branch": branch, "label": label, "sub": sub, "link": link, "held": held, "calc": calc,
            "ideal": ideal, "status": status, "word": word, "colour": colour, "detail": detail, "fix": fix, "note": note}


def branch_editor(level, key, title=None):
    """Editor context for one brick group (or product) branch by branch: used on the group page and on Buy more /
    Too much / Stop stocking / review recommendations."""
    from kkj.brain import fixes
    level = level if level in fixes.FIX_LEVELS else "group"
    col = fixes.COLUMN[level]
    from kkj.metrics import ideal_stock as ist
    rows = read(f"""SELECT scope, sum(current_pcs) AS held, sum(coalesce(calc_pcs, ideal_pcs)) AS calc,
                           sum(ideal_pcs) AS ideal, sum(annual) AS annual,
                           bool_and(current_pcs <= 0 OR (coalesce(display_months, 0) >= :m AND coalesce(sold_12m, 0) <= 0)) AS dead
                    FROM brain.ideal_stock WHERE {LATEST_IDEAL} AND scope <> 'Company' AND {col} = :k
                    GROUP BY scope""", k=key, m=_rules()["stop_display_months"])
    rules = _rules()
    fx = _fixes()
    mine = fx[(fx["level"] == level) & (fx["key"] == key)]
    by_branch = {r["branch"]: _fix_info(r) for r in mine.to_dict("records")}
    out = []
    for r in rows.sort_values(["ideal", "held"], ascending=False).itertuples():
        if not (r.held or r.ideal or r.calc):
            continue
        out.append(_editor_row(key, r.scope, _short(r.scope), r.held, r.calc, r.ideal,
                               str(ist.status([float(r.held or 0)], [float(r.ideal or 0)], rules, [bool(r.dead)])[0]),
                               by_branch.get(r.scope),
                               sub=f"{r.annual:.1f} / yr forecast" if r.annual and r.annual >= 0.05 else "no forecast sales"))
    others = [_fix_info(f) for f in mine.to_dict("records") if f["branch"] == "*"]
    if level == "group" and len(rows):
        items = set(read(f"SELECT DISTINCT item_name FROM brain.ideal_stock WHERE {LATEST_IDEAL} AND brick_group = :k",
                         k=key)["item_name"])
        others += [_fix_info(f) for f in fx.to_dict("records") if f["level"] == "product" and f["key"] in items]
    total = {"held": sum(r["held"] for r in out), "calc": sum(r["calc"] for r in out), "ideal": sum(r["ideal"] for r in out)}
    return {"id": "ed-branches", "level": level, "mode": "branches", "rows": out, "total": total, "others": others,
            "title": title or "By branch", "what": "Branch", "reasons": REASONS,
            "review_days": rules["override_review_days"]}


@router.get("/ideal-stock", response_class=HTMLResponse)
def ideal_stock(request: Request, scope: str = "Company", group: list[str] = Query(default=[]),
                status: list[str] = Query(default=[]), search: str = "", view: str = "product"):
    from kkj.metrics import ideal_stock as ist
    rules = _rules()
    view = view if view in ("product", "group") else "product"
    t = read(f"SELECT * FROM brain.ideal_stock WHERE {LATEST_IDEAL}")
    if t.empty:
        return render(request, "ideal_stock.html", "ideal_stock", empty=True, f={}, rules=rules)
    as_of = t["as_of"].iloc[0]
    value = read("""SELECT brick, avg_piece_value FROM brain.brick_turn WHERE level = 'group' AND scope = 'Company'
                    AND as_of = (SELECT max(as_of) FROM brain.brick_turn)""").set_index("brick")["avg_piece_value"]
    t["unit_value"] = t["brick_group"].map(value).astype(float).fillna(float(value.median() or 0))
    for c in ("current_pcs", "ideal_pcs", "change_pcs", "annual", "calc_pcs"):
        t[c] = t[c].astype(float)
    t["held_value"], t["ideal_value"] = t["current_pcs"] * t["unit_value"], t["ideal_pcs"] * t["unit_value"]
    t["change_value"] = t["change_pcs"] * t["unit_value"]
    scopes = ["Company"] + sorted(x for x in t["scope"].unique() if x != "Company")
    scope = scope if scope in scopes else "Company"
    t = t[t["scope"] == scope]
    groups = sorted(t["item_class_group"].dropna().unique())
    group, status = [g for g in group if g], [x for x in status if x in IDEAL_STATUS]
    if group:
        t = t[t["item_class_group"].isin(group)]
    if search:
        t = t[t["brick_group"].str.contains(search, case=False, regex=False)
              | t["item_name"].fillna("").str.contains(search, case=False, regex=False)]
    if view == "product":
        g = t.groupby("item_name", dropna=False).agg(held_value=("held_value", "sum"), ideal_value=("ideal_value", "sum"))
        t = ist.by_product(t, rules).merge(g, on="item_name", how="left")
        t["unit_value"] = (t["held_value"] + t["ideal_value"]) / (t["current_pcs"] + t["ideal_pcs"]).where(
            (t["current_pcs"] + t["ideal_pcs"]) > 0)
        t["change_value"] = t["change_pcs"] * t["unit_value"].fillna(0)
        t["brick_group"] = t["item_name"].fillna("(unknown)")

    by = t.groupby("status").agg(n=("brick_group", "size"), held=("held_value", "sum"), change=("change_value", "sum"))
    tiles = []
    for key, (word, colour) in IDEAL_STATUS.items():
        n = int(by.loc[key, "n"]) if key in by.index else 0
        if key == "increase":
            sub = f"{money(by.loc[key, 'change'])} to add" if n else ""
        elif key == "reduce":
            sub = f"{money(-by.loc[key, 'change'])} over" if n else ""
        elif key in ("phase_out", "unproven"):
            sub = f"{money(by.loc[key, 'held'])} held" if n else ""
        else:
            sub = f"{money(by.loc[key, 'held'])} held" if n else ""
        tiles.append({"key": key, "word": word, "colour": colour, "n": n, "sub": sub, "on": key in status})
    held, ideal = t["held_value"].sum(), t["ideal_value"].sum()

    fx = _fixes()
    level_branch = "*" if scope == "Company" else scope
    exact = {f["key"]: _fix_info(f) for f in fx.to_dict("records") if f["level"] == view and f["branch"] == level_branch}
    touched = set(fx[fx["level"] == view]["key"])
    shown = t[t["status"].isin(status)] if status else t
    shown = shown.assign(order=shown["change_value"].abs().where(shown["status"] != "phase_out", shown["held_value"]))
    shown = shown.sort_values("order", ascending=False).head(400)
    rows = []
    for r in shown.itertuples():
        fix = exact.get(r.brick_group)
        note = "set by hand at a branch" if not fix and r.brick_group in touched and r.ideal_pcs != r.calc_pcs else None
        sells = f"{r.annual:.1f} / yr" if r.annual and r.annual >= 0.05 else "no forecast sales"
        if view == "product":
            sub = f"{r.item_class_group or ''} · {sells} · {r.groups} group{'s' if r.groups != 1 else ''}"
            link = f"/ideal-stock/group?level=product&g={quote(r.brick_group)}"
        else:
            sub, link = f"{r.item_class_group or ''} · {sells}", f"/ideal-stock/group?g={quote(r.brick_group)}"
        rows.append(_editor_row(r.brick_group, level_branch, r.brick_group, r.current_pcs, r.calc_pcs, r.ideal_pcs,
                                r.status, fix, sub=sub, link=link, note=note))
    n_total = len(t[t["status"].isin(status)] if status else t)
    editor = {"id": "ed-groups", "level": view, "mode": "groups", "rows": rows, "reasons": REASONS,
              "what": "Product" if view == "product" else "Group", "review_days": rules["override_review_days"],
              "n_total": n_total,
              "title": "All branches" if scope == "Company" else _short(scope)}

    # forecast accuracy (for the How it works section)
    acc = read("""SELECT level, key, horizon_months, origins, actual, forecast, wape, wape_last_year, wape_avg12, bias, better
                  FROM brain.forecast_accuracy WHERE as_of = (SELECT max(as_of) FROM brain.forecast_accuracy)""")
    def weighted(level, label):
        d = acc[(acc["level"] == level) & acc["wape"].notna()]
        if not len(d) or not d["actual"].sum():
            return None
        return {"key": label, "actual": d["actual"].sum(), "forecast": d["forecast"].sum(),
                "accuracy": 1 - (d["wape"] * d["actual"]).sum() / d["actual"].sum(),
                "bias": d["forecast"].sum() / d["actual"].sum() - 1}
    levels = [weighted("company", "Whole company"), weighted("item_group", "By item group"),
              weighted("product", "By product"), weighted("product_branch", "By product at a branch")]
    lv = pd.DataFrame([x for x in levels if x])
    acc_table = charts.table(lv[["key", "actual", "accuracy", "bias"]] if len(lv) else lv,
                             {"actual": "num", "accuracy": "pct", "bias": "pct"},
                             {"key": "", "actual": "pieces sold", "accuracy": "accuracy", "bias": "forecast vs sold"})
    band = acc[acc["level"] == "band"].assign(ratio=lambda d: d["actual"] / d["forecast"].where(d["forecast"] > 0))
    band_table = charts.table(band[["key", "forecast", "actual", "ratio"]] if len(band) else band,
                              {"forecast": "num", "actual": "num", "ratio": "dec"},
                              {"key": "forecast per product at a branch", "forecast": "forecast", "actual": "sold",
                               "ratio": "sold ÷ forecast"})
    fine_err = None
    horizon = int(acc["horizon_months"].iloc[0]) if len(acc) else None
    origins = int(acc["origins"].max()) if len(acc) else None

    from kkj.brain import fixes
    latest = read(f"""SELECT scope, brick_group, item_name, calc_pcs, ideal_pcs FROM brain.ideal_stock
                      WHERE {LATEST_IDEAL} AND scope <> 'Company'""")
    learn = fixes.suggestions(fx, latest, rules["choice_months"])
    mine = [_fix_info(f) for f in fx.to_dict("records")]
    return render(request, "ideal_stock.html", "ideal_stock", empty=False, rules=rules, as_of=as_of, tiles=tiles,
                  held=held, ideal=ideal, scopes=scopes, groups=groups,
                  statuses={k: v[0] for k, v in IDEAL_STATUS.items()},
                  f={"scope": scope, "group": group, "status": status, "search": search}, ed=editor,
                  acc=acc_table, band=band_table, fine_err=fine_err, horizon=horizon, origins=origins, learn=learn, mine=mine,
                  due=[m for m in mine if m["due"]],
                  view=view,
                  base_q=urlencode([("scope", scope), ("view", view)] + [("group", g) for g in group]
                                   + ([("search", search)] if search else [])))


def _back(form):
    back = str(form.get("back") or "/ideal-stock")
    return back if back.startswith("/") and not back.startswith("//") else "/ideal-stock"


def _done(back, msg):
    from urllib.parse import parse_qsl, urlencode, urlsplit
    parts = urlsplit(back)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "saved"] + [("saved", msg)]
    return HTMLResponse("", headers={"HX-Redirect": f"{parts.path}?{urlencode(query)}"})


def _fail(error):
    return HTMLResponse(f'<div class="alert alert-danger mb-0"><i class="ti ti-alert-circle"></i> {html_escape(error)}</div>')


@router.post("/ideal-stock/save", response_class=HTMLResponse)
async def save_ideals(request: Request):
    """Numbers typed into an ideal-stock table, saved together with one reason (kkj.brain.fixes.save_many)."""
    from kkj.brain import fixes
    form = await request.form()
    keys, branches, origs, pcs = (form.getlist(n) for n in ("key", "branch", "orig", "pcs"))
    changes = [(k, b, p) for k, b, o, p in zip(keys, branches, origs, pcs) if str(p).strip() != str(o).strip()]
    with engine().begin() as conn:
        msg, error = fixes.save_many(conn, form.get("level", "group"), changes, form.get("reason"), who(request),
                                     rules_mod.load(conn))
    return _fail(error) if error else _done(_back(form), msg)


@router.post("/ideal-stock/reset", response_class=HTMLResponse)
async def reset_ideal(request: Request):
    """Back to the suggested (calculated) number for one fix."""
    from kkj.brain import fixes
    form = await request.form()
    level, key, branch = form.get("reset_level", ""), form.get("reset_key", ""), form.get("reset_branch", "")
    if level not in fixes.FIX_LEVELS or not key or not branch:
        return _fail("Choose which number to set back.")
    with engine().begin() as conn:
        msg = fixes.reset(conn, level, key, branch, who(request), rules_mod.load(conn))
    return _done(_back(form), msg)


@router.get("/ideal-stock/group", response_class=HTMLResponse)
def ideal_group(request: Request, g: str = "", level: str = "group"):
    """One brick group (or product): its ideal at each branch to check or change, and how the suggestion is made."""
    from kkj.brain import fixes
    level = level if level in fixes.FIX_LEVELS else "group"
    col = fixes.COLUMN[level]
    rows = read(f"""SELECT * FROM brain.ideal_stock WHERE {LATEST_IDEAL} AND scope <> 'Company' AND {col} = :k""", k=g)
    if rows.empty:
        return render(request, "ideal_group.html", "ideal_stock", empty=True, g=g, level=level)
    rows = rows[(rows["current_pcs"] > 0) | (rows["ideal_pcs"] > 0) | (rows["calc_pcs"].fillna(0) > 0)]
    rows = rows.sort_values(["ideal_pcs", "current_pcs"], ascending=False)
    reason = {"demand": "cover", "choice": "choice", "display minimum": "display", "override": "set by hand", "no demand": "–"}
    why = rows["calc_from"].where(rows["calc_from"].notna(), rows["ideal_from"]) if "calc_from" in rows else rows["ideal_from"]
    rows = rows.assign(why=why.map(lambda x: reason.get(x, x)))
    if level == "product":
        # decided per product at each branch: demand, choice and display added up over its groups
        for c in ("held_12m", "annual_at_ideal"):
            if c not in rows:
                rows[c] = np.nan
        pb = rows.groupby("scope").agg(current_pcs=("current_pcs", "sum"), held_12m=("held_12m", "first"),
                                       annual=("annual", "sum"), for_demand=("for_demand", "first"),
                                       for_choice=("for_choice", "first"), display_min=("display_min", "sum"),
                                       product_ideal=("product_ideal", "first"), annual_at_ideal=("annual_at_ideal", "sum"),
                                       why=("why", "first")).reset_index().sort_values("product_ideal", ascending=False)
        table = charts.table(pb.assign(scope=pb["scope"].map(_short)),
                             {"current_pcs": "num", "held_12m": "dec", "annual": "dec", "for_demand": "num",
                              "for_choice": "num", "display_min": "num", "product_ideal": "num", "annual_at_ideal": "dec"},
                             {"scope": "branch", "current_pcs": "held", "held_12m": "held, 12m avg", "annual": "forecast 12m",
                              "for_demand": "cover", "for_choice": "choice", "display_min": "display",
                              "product_ideal": "suggested", "annual_at_ideal": "sales at suggested", "why": "driven by"})
    else:
        table = charts.table(
            rows[["scope", "current_pcs", "annual", "mix_share", "product_ideal", "calc_pcs", "display_months",
                  "sold_12m"]].assign(scope=rows["scope"].map(_short)),
            {"current_pcs": "num", "annual": "dec", "mix_share": "pct", "product_ideal": "num", "calc_pcs": "num",
             "display_months": "num", "sold_12m": "num"},
            {"scope": "branch", "current_pcs": "held", "annual": "forecast 12m", "mix_share": "share of product",
             "product_ideal": "product ideal", "calc_pcs": "suggested", "display_months": "months on display",
             "sold_12m": "sold 12m"})
    first = rows.iloc[0]
    acc = read("""SELECT level, key, wape, wape_last_year, wape_avg12, bias, better, actual FROM brain.forecast_accuracy
                  WHERE as_of = (SELECT max(as_of) FROM brain.forecast_accuracy)
                  AND ((level = 'product' AND key = :p) OR (level = 'item_group' AND key = :i))""",
               p=first["item_name"], i=first["item_class_group"]).to_dict("records")
    rules = _rules()
    history = read("""SELECT changed_at AT TIME ZONE 'Asia/Kolkata' AS changed_at, changed_by, row_key, old_value, new_value, note
                      FROM config.change_log WHERE table_name = 'ideal_stock_override' AND row_key LIKE :k
                      ORDER BY change_id DESC LIMIT 30""", k=f"%: {g} @ %").to_dict("records")
    return render(request, "ideal_group.html", "ideal_stock", empty=False, g=g, level=level, t=table, acc=acc,
                  rules=rules, first=first.to_dict(), history=history, ed=branch_editor(level, g),
                  annual=float(rows["annual"].sum()))


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
    kpis = [k("increase", "ti-trending-up", "green", lambda r: f"{money(r['change'])} to add"),
            k("reduce", "ti-trending-down", "orange", lambda r: f"{money(-r['change'])} over"),
            k("keep", "ti-check", "blue", lambda r: f"{money(r['value'])} held"),
            k("phase_out", "ti-trash", "red", lambda r: f"{money(r['value'])} held"),
            ("Too new to judge", f"{int(by_status.reindex(['new', 'hold', 'stock_up', 'no_history'])['bricks'].sum()):,} bricks",
             f"{money(by_status.reindex(['new', 'hold', 'stock_up', 'no_history'])['value'].sum())} held", "ti-eye", "secondary")]

    chart = by_status.reset_index()
    chart["label"] = chart["status"].map(lambda s: bt.STATUS[s][0])
    shown = t[t["status"].isin(status)] if status else t
    shown = shown.assign(order=shown["change_value"].abs().where(shown["status"] != "phase_out", shown["current_value"]))
    shown = shown.sort_values("order", ascending=False).head(300)
    def turns(r):
        return " · ".join("–" if pd.isna(r[c]) else ("∞" if np.isinf(r[c]) else f"{r[c]:.2f}") for c in ("turn_3m", "turn_6m", "turn_12m"))
    shown = shown.assign(
        status_label=shown["status"].map(lambda s: bt.STATUS[s][0]),
        windows=shown.apply(turns, axis=1),
        ideal=shown.apply(lambda r: f"{r['ideal_turn']:.2f}×", axis=1))
    table = charts.table(
        shown[["brick", "current_pcs", "ideal_pcs", "change_pcs", "change_value", "blended_turn", "ideal", "windows",
               "status_label"]],
        {"current_pcs": "num", "ideal_pcs": "num", "change_pcs": "num", "change_value": "money", "blended_turn": "dec"},
        {"brick": "brick group" if view == "group" else "brick", "current_pcs": "now (pcs)", "ideal_pcs": "ideal (pcs)",
         "change_pcs": "change (pcs)", "change_value": "change (₹)", "blended_turn": "turn (× a year)",
         "ideal": "turn at ideal stock", "windows": "turn 3 · 6 · 12 months", "status_label": "what to do"})
    return render(request, "stock_turn.html", "stock_turn", empty=False, rules=rules, as_of=as_of, kpis=kpis,
                  scopes=scopes, groups=groups, statuses=bt.STATUS, f={"view": view, "scope": scope, "group": group, "status": status, "search": search},
                  fig=charts.hbar(chart["label"], chart["value"] / 1e7, np.c_[chart["bricks"], chart["pcs"]],
                                  "<br>%{customdata[0]:,.0f} bricks · %{customdata[1]:,.0f} pcs"),
                  t=table, n_shown=len(shown), n_total=len(t[t["status"].isin(status)] if status else t),
                  levels=" – ".join(bt.LEVEL_LABELS[lv] for lv in bt.LEVELS
                                    if lv in rules["turn_brick_levels" if view == "brick" else "turn_group_levels"]),
)


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
    latest = cached(q.stock_latest)
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
              + ", ".join(f"{y}-{str(y + 1)[2:]}" for y in years) + f" · {daily['pcs'].sum():,.0f} pieces, diamond item groups")
    return render(request, "seasonality.html", "seasonality", empty=False, rules=rules, period=period,
                  fig_groups=fig_groups, t_groups=t_groups, groups=groups, f={"group": group}, t_products=t_products,
                  t_lift=t_lift, upcoming=upcoming.astype(object).where(upcoming.notna(), None).to_dict("records"), t_outlook=t_outlook, t_ahead=t_ahead,
                  as_of=as_of)
