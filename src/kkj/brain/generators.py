"""
Decision generators: current stock + recent sales + business rules -> candidate decisions.

Pure functions. Each returns a list of dicts ready for kkj.brain.store (action_key, kind,
title, summary, branch, subject, impact_value, impact_label, confidence, evidence, pieces).
Everything is measured per GROUP of similar pieces (the cluster_dimensions rule, e.g.
item x price band x carat band) at each SHOWROOM, from stock that is on a counter.

Inputs (DataFrames):
  stock:  latest snapshot, company stock: barcode, branch, location, item_class_group, item_name,
          category, supplier, pcs, dia_wt, net_wt, rate, dia_amt, value, age_days
  sales:  sale lines (returns excluded) over the demand window: sale_date, branch, item_class_group,
          item_name, category, supplier, salesman, pcs, item_amt, taxable_amount, dia_wt
  showrooms: list of branch names that trade stock with each other
"""
import math

import numpy as np
import pandas as pd

from kkj.metrics import core as m

KIND_WEIGHT = {"understock": 1.0, "transfer": 1.0, "discount": 0.9, "offer": 0.8, "liquidate": 0.7,
               "supplier_slow": 0.6, "pause_buying": 0.6, "supplier_fast": 0.5, "buy_band": 0.5}


# ── groups of similar pieces ──────────────────────────────────────────────────
def _band(values, edges, fmt):
    edges = list(edges)
    labels = [f"< {fmt(edges[0])}"] + [f"{fmt(a)}–{fmt(b)}" for a, b in zip(edges[:-1], edges[1:])] + [f"{fmt(edges[-1])} +"]
    return pd.cut(pd.Series(values, dtype="float"), [-np.inf] + edges + [np.inf], labels=labels, right=False).astype(str)


def band_money(x):
    """Compact band edge: 50000 -> '₹50K', 200000 -> '₹2L'."""
    return f"₹{x / 1e5:g}L" if x >= 1e5 else f"₹{x / 1e3:g}K"


def money(x):
    """An amount for people: '₹6.59 Cr', '₹22.4 L', '₹48K'."""
    x = float(x or 0)
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.1f} L"
    return f"₹{x / 1e3:.0f}K" if abs(x) >= 1e3 else f"₹{x:.0f}"


def carats(x):
    return f"{x:g}ct"


def group_key(df, rules, value_col):
    """'D RING · ₹50K–₹1L · 0.25ct–0.5ct' per row, from the cluster_dimensions rule."""
    parts = []
    for dim in rules["cluster_dimensions"]:
        if dim == "price_band":
            parts.append(_band(df[value_col], rules["price_band_edges"], band_money).values)
        elif dim == "ct_band":
            parts.append(_band(df["dia_wt"].fillna(0), rules["ct_band_edges"], carats).values)
        elif dim in df:
            parts.append(df[dim].fillna("–").astype(str).values)
    return pd.Series([" · ".join(p) for p in zip(*parts)], index=df.index) if parts else pd.Series("all", index=df.index)


def confidence(sold_pcs):
    return "high" if sold_pcs >= 10 else "medium" if sold_pcs >= 4 else "low"


def _priority(kind, impact, conf):
    return round(KIND_WEIGHT.get(kind, 0.5) * math.log10(max(impact or 0, 0) + 1) *
                 {"high": 1.0, "medium": 0.85, "low": 0.7}[conf], 4)


def _action(kind, key, title, summary, impact, impact_label, conf, evidence, branch=None, subject=None, pieces=None):
    return {"action_key": f"{kind}|{key}", "kind": kind, "title": title, "summary": summary, "branch": branch,
            "subject": subject or {}, "impact_value": round(float(impact or 0), 2), "impact_label": impact_label,
            "priority": _priority(kind, impact, conf), "confidence": conf, "evidence": evidence, "pieces": pieces}


def _piece_list(df):
    cols = ["barcode", "item_name", "location", "age_days", "dia_wt", "value"]
    return df[[c for c in cols if c in df]].assign(value=lambda d: d["value"].round(0)).to_dict("records")


def short(branch):
    return str(branch).split(" / ")[0]


# ── demand per group and showroom ─────────────────────────────────────────────
def demand_table(stock, sales, rules, showrooms):
    """One row per (group, showroom): pieces on counters, sold in the window, days of stock."""
    window = rules["demand_window_days"]
    st = stock[stock["branch"].isin(showrooms)].copy()
    st = st[m.location_type(st["location"]) == "Counter"]
    st["group"] = group_key(st, rules, "value")
    sa = sales[sales["branch"].isin(showrooms)].copy()
    sa["group"] = group_key(sa, rules, "item_amt")
    held = st.groupby(["group", "branch"]).agg(pcs=("pcs", "sum"), value=("value", "sum"))
    sold = sa.groupby(["group", "branch"]).agg(sold_pcs=("pcs", "sum"), sold_value=("item_amt", "sum"))
    t = held.join(sold, how="outer").fillna(0).reset_index()
    t["per_day"] = t["sold_pcs"] / window
    t["days_of_stock"] = np.where(t["per_day"] > 0, t["pcs"] / t["per_day"], np.where(t["pcs"] > 0, np.inf, 0))
    t["avg_piece_value"] = np.where(t["pcs"] > 0, t["value"] / t["pcs"].clip(lower=1),
                                    np.where(t["sold_pcs"] > 0, t["sold_value"] / t["sold_pcs"].clip(lower=1), 0))
    return t, st


# ── transfers and understock ──────────────────────────────────────────────────
def transfers_and_understock(stock, sales, rules, showrooms):
    t, st = demand_table(stock, sales, rules, [b for b in showrooms if b not in rules["transfer_excluded_branches"]])
    fill_to, over, under = rules["transfer_fill_to_days"], rules["overstock_days"], rules["understock_days"]
    min_sold, window = rules["min_sold_to_count_as_selling"], rules["demand_window_days"]
    no_buy_items = set(st.loc[st["item_class_group"].isin(rules["no_buy_groups"]), "item_name"].dropna())
    order = {"oldest": ("age_days", False), "lowest_value": ("value", True), "highest_value": ("value", False)}
    sort_col, ascending = order[rules["transfer_piece_priority"]]
    actions = []
    for group, g in t.groupby("group"):
        g = g.set_index("branch")
        selling = g["sold_pcs"] >= min_sold
        need = (np.ceil(g["per_day"] * fill_to) - g["pcs"]).where(selling & (g["days_of_stock"] < fill_to), 0).clip(lower=0)
        surplus = (g["pcs"] - np.ceil(g["per_day"] * fill_to)).where(g["days_of_stock"] > over, 0).clip(lower=0)
        receivers = need[need > 0].sort_values(ascending=False)
        donors = surplus[surplus > 0].sort_values(ascending=False)
        for receiver, wanted in receivers.items():
            wanted = int(wanted)
            if rules["transfer_enabled"]:
                for donor in list(donors.index):
                    if wanted <= 0 or donor == receiver:
                        continue
                    n = int(min(wanted, donors[donor]))
                    pool = st[(st["group"] == group) & (st["branch"] == donor)].sort_values(sort_col, ascending=ascending)
                    pieces = pool.head(n)
                    if pieces.empty or pieces["value"].sum() < rules["transfer_min_value"]:
                        continue
                    donors[donor] -= len(pieces)
                    wanted -= len(pieces)
                    r, d = g.loc[receiver], g.loc[donor]
                    actions.append(_action(
                        "transfer", f"{group}|{donor}|{receiver}",
                        f"Move {len(pieces)} {group} from {short(donor)} to {short(receiver)}",
                        f"{short(receiver)} sells {r['sold_pcs']:.0f} in {window} days and holds {r['pcs']:.0f} "
                        f"({r['days_of_stock']:.0f} days of stock); {short(donor)} holds {d['pcs']:.0f} and sold "
                        f"{d['sold_pcs']:.0f} ({'no sales' if np.isinf(d['days_of_stock']) else f'{d.days_of_stock:.0f} days of stock'}).",
                        pieces["value"].sum(), f"{money(pieces['value'].sum())} of stock moved to where it sells",
                        confidence(r["sold_pcs"]),
                        {"group": group, "window_days": window, "receiver": r[["pcs", "sold_pcs", "days_of_stock"]].to_dict(),
                         "donor": d[["pcs", "sold_pcs", "days_of_stock"]].to_dict()},
                        branch=receiver, subject={"group": group, "from": donor, "to": receiver}, pieces=_piece_list(pieces)))
            r = g.loc[receiver]
            buyable = not any(group.startswith(f"{n} ") or f" {n} " in f" {group} " for n in no_buy_items)
            if wanted > 0 and r["days_of_stock"] < under and buyable:
                actions.append(_action(
                    "understock", f"{group}|{receiver}",
                    f"{short(receiver)} is running short of {group}: buy {wanted}",
                    f"Sells {r['sold_pcs']:.0f} in {window} days ({r['per_day'] * 30.44:.1f} a month) and holds "
                    f"{r['pcs']:.0f}: {r['days_of_stock']:.0f} days of stock. No other showroom has spare to send.",
                    r["per_day"] * 91 * r["avg_piece_value"],
                    f"≈{money(r['per_day'] * 30.44 * r['avg_piece_value'])} of sales a month at risk",
                    confidence(r["sold_pcs"]),
                    {"group": group, "window_days": window, "pcs": r["pcs"], "sold_pcs": r["sold_pcs"],
                     "days_of_stock": r["days_of_stock"], "suggested_qty": wanted},
                    branch=receiver, subject={"group": group, "qty": wanted}))
    return actions


# ── aged stock: offers ────────────────────────────────────────────────────────
def suggested_discount(age_days, rules):
    """Deeper for older pieces: 5% up to a year, 10% to two years, then the maximum allowed."""
    cap = rules["max_offer_discount_pct"]
    return min(cap, 5 if age_days <= 365 else 10 if age_days <= 730 else cap)


def offers(stock, sales, rules, showrooms, exclude_barcodes=()):
    t, st = demand_table(stock, sales, rules, showrooms)
    slow = t[t["days_of_stock"] > rules["overstock_days"]][["group", "branch", "sold_pcs", "days_of_stock"]]
    aged = st[(st["age_days"] > rules["aged_days"]) & (st["age_days"] <= rules["liquidation_age_days"])
              & ~st["barcode"].isin(set(exclude_barcodes))].merge(slow, on=["group", "branch"])
    actions = []
    for (branch, item), g in aged.groupby(["branch", "item_name"]):
        g = g.assign(offer_pct=g["age_days"].map(lambda a: suggested_discount(a, rules)))
        value = g["value"].sum()
        actions.append(_action(
            "offer", f"{branch}|{item}",
            f"Put {len(g)} aged {item} at {short(branch)} on offer",
            f"{len(g)} pieces over {rules['aged_days']} days old in groups that hold more than "
            f"{rules['overstock_days']} days of stock at this showroom. Suggested offer by age: "
            f"{g['offer_pct'].min():g}%–{g['offer_pct'].max():g}%.",
            value, f"{money(value)} of aged stock to turn into cash",
            confidence(g["sold_pcs"].max()),
            {"pieces": len(g), "median_age_days": float(g["age_days"].median()), "groups": sorted(g["group"].unique())},
            branch=branch, subject={"item": item},
            pieces=g.assign(offer_pct=g["offer_pct"])[["barcode", "group", "location", "age_days", "value", "offer_pct"]]
            .assign(value=lambda d: d["value"].round(0)).to_dict("records")))
    return actions


# ── liquidation ───────────────────────────────────────────────────────────────
def liquidation(stock, rules):
    from kkj.metrics.insights import recovery_value
    old = stock[stock["age_days"] > rules["liquidation_age_days"]].copy()
    old["recovery"] = recovery_value(old, rules["diamond_recovery_pct"] / 100)
    actions = []
    for branch, g in old.groupby("branch"):
        value, rec = g["value"].sum(), g["recovery"].sum()
        actions.append(_action(
            "liquidate", f"{branch}",
            f"Decide on {len(g)} pieces over {rules['liquidation_age_days'] // 365} years old at {short(branch)}",
            f"These almost never sell now. Tag value {money(value)}, recovery value about {money(rec)} "
            f"({100 * rec / value:.0f}%): choose between breaking up, returning to the supplier, an exhibition or a final offer.",
            value, f"{money(value)} tied up; ≈{money(rec)} recoverable", "high",
            {"pieces": len(g), "tag_value": value, "recovery_value": rec},
            branch=branch, pieces=g.assign(recovery=g["recovery"].round(0))[
                ["barcode", "item_name", "location", "supplier", "age_days", "value", "recovery"]]
            .assign(value=lambda d: d["value"].round(0)).to_dict("records")))
    return actions


# ── suppliers ─────────────────────────────────────────────────────────────────
def suppliers(scorecard, stock):
    """scorecard: kkj.metrics.insights.supplier_scorecard output."""
    held_now = stock.groupby("supplier")["value"].sum()
    actions = []
    for name, s in scorecard.dropna(subset=["performance"]).iterrows():
        conf = confidence(s["sold_pcs"])
        ev = {"performance": s["performance"], "monthly_sell_rate": s["monthly_sell_rate"], "avg_pcs": s["avg_pcs"],
              "annual_turn": s["annual_turn"], "median_days_to_sell": s["median_days_to_sell"],
              "aged_share_now": s["aged_share_now"]}
        if s["performance"] < 0.5:
            held = held_now.get(name, 0)
            actions.append(_action(
                "supplier_slow", name, f"Talk to {name} about exchanges or returns",
                f"Their pieces sell at {s['performance']:.2f}× the company's rate at the same prices "
                f"({100 * s['monthly_sell_rate']:.1f}% a month); {100 * (s['aged_share_now'] or 0):.0f}% of what we hold "
                f"from them is over a year old. Hold back new orders until it moves.",
                held, f"{money(held)} held from this supplier", conf, ev, subject={"supplier": name}))
        elif s["performance"] > 1.5:
            monthly = s["sold_per_month"] * (s["avg_value"] / max(s["avg_pcs"], 1))
            actions.append(_action(
                "supplier_fast", name, f"Buy more from {name}",
                f"Their pieces sell at {s['performance']:.2f}× the company's rate at the same prices "
                f"({100 * s['monthly_sell_rate']:.1f}% a month, median {s['median_days_to_sell']:.0f} days to sell).",
                monthly, f"≈{money(monthly)} of sales a month from their pieces", conf, ev, subject={"supplier": name}))
    return actions


# ── buying direction ──────────────────────────────────────────────────────────
def buying(stock, sales, rules):
    window = rules["demand_window_days"]
    months = window / 30.44
    stock = stock[~stock["item_class_group"].isin(rules["no_buy_groups"])]
    sales = sales[~sales["item_class_group"].isin(rules["no_buy_groups"])]
    held = stock.groupby("item_name").agg(pcs=("pcs", "sum"), value=("value", "sum"))
    sold = sales.groupby("item_name")["pcs"].sum().rename("sold_pcs")
    t = held.join(sold, how="left").fillna(0)
    t["months_of_stock"] = np.where(t["sold_pcs"] > 0, t["pcs"] / (t["sold_pcs"] / months), np.inf)
    limit = rules["overstock_days"] / 30.44
    actions = []
    for item, r in t[(t["months_of_stock"] > limit) & (t["pcs"] >= 20)].iterrows():
        excess_pcs = r["pcs"] - (r["sold_pcs"] / months) * limit
        excess = excess_pcs * r["value"] / r["pcs"]
        actions.append(_action(
            "pause_buying", item, f"Pause buying {item}",
            f"Holding {r['pcs']:.0f} pieces against {r['sold_pcs']:.0f} sold in {window} days: "
            f"{'no sales' if np.isinf(r['months_of_stock']) else f'{r.months_of_stock:.0f} months of stock'} "
            f"(limit {limit:.0f}).", excess, f"≈{money(excess)} more than {limit:.0f} months needs",
            confidence(r["sold_pcs"]), {"pcs": r["pcs"], "sold_pcs": r["sold_pcs"], "months_of_stock": r["months_of_stock"]},
            subject={"item": item}))
    # price bands: where sales outgrow stock
    edges = rules["price_band_edges"]
    s_band = _band(stock["value"], edges, band_money)
    sa_band = _band(sales["item_amt"], edges, band_money)
    share = pd.DataFrame({"stock": stock.groupby(s_band.values)["pcs"].sum() / stock["pcs"].sum(),
                          "sales": sales.groupby(sa_band.values)["pcs"].sum() / max(sales["pcs"].sum(), 1)}).fillna(0)
    share["gap"] = share["sales"] - share["stock"]
    under = share[share["gap"] > 0.03].sort_values("gap", ascending=False)
    if len(under):
        over = share[share["gap"] < -0.03]
        over_value = stock[s_band.isin(over.index).values]["value"].sum()
        actions.append(_action(
            "buy_band", "price_bands", f"Shift buying towards {', '.join(under.index)}",
            "; ".join(f"{b}: {100 * r['sales']:.0f}% of sales vs {100 * r['stock']:.0f}% of stock" for b, r in under.iterrows())
            + (f". Over-held: {', '.join(over.index)}." if len(over) else ""),
            over_value, f"{money(over_value)} sits in price bands that sell less than their share",
            confidence(sales["pcs"].sum()), share.round(3).to_dict("index"), subject={"bands": list(under.index)}))
    return actions


# ── discounts ─────────────────────────────────────────────────────────────────
def discounts(stock, sales, rules, people):
    """Discount given on fast-selling groups, by salesperson, vs the team."""
    s = sales[sales["taxable_amount"].notna() & (sales["item_amt"] > 0)].copy()
    if s.empty:
        return []
    window = rules["demand_window_days"]
    s["group"] = group_key(s, rules, "item_amt")
    st = stock.assign(group=group_key(stock, rules, "value"))
    held = st.groupby("group")["pcs"].sum()
    rate = s.groupby("group")["pcs"].sum() / window
    days = (held.reindex(rate.index).fillna(0) / rate).replace([np.inf], np.nan)
    fast = set(days[days < rules["fast_group_days"]].index)
    s = s[s["group"].isin(fast)]
    s["discount"] = (s["item_amt"] - s["taxable_amount"]).clip(lower=0)
    sellers = people[(people["kind"] == "salesperson") & people["active"]]
    s["code"] = s["salesman"].map(lambda v: _code(v))
    s = s[s["code"].isin(sellers["sales_code"])]
    if s.empty:
        return []
    team_rate = s["discount"].sum() / s["item_amt"].sum()
    by = s.groupby("code").agg(lines=("pcs", "size"), discount=("discount", "sum"), label=("item_amt", "sum"))
    by["rate"] = by["discount"] / by["label"]
    names = sellers.set_index("sales_code")
    actions = []
    for code, r in by[(by["lines"] >= 8) & (by["rate"] > team_rate + 0.02)].iterrows():
        avoidable = r["discount"] - team_rate * r["label"]
        if avoidable < 25_000:
            continue
        name = names.at[code, "name"] if code in names.index else code
        actions.append(_action(
            "discount", code, f"Review discounting by {name}",
            f"On fast-selling pieces (groups with under {rules['fast_group_days']} days of stock) {name} gave "
            f"{100 * r['rate']:.1f}% off across {r['lines']:.0f} lines in {window} days, against {100 * team_rate:.1f}% "
            f"for the team.", avoidable, f"≈{money(avoidable)} more discount than the team rate",
            confidence(r["lines"]), {"lines": r["lines"], "rate": r["rate"], "team_rate": team_rate,
                                     "discount": r["discount"]},
            branch=names.at[code, "branch"] if code in names.index else None, subject={"salesperson": code}))
    return actions


def _code(value):
    from kkj.salespeople import split_salesman
    return split_salesman(value)[0] if isinstance(value, str) else None
