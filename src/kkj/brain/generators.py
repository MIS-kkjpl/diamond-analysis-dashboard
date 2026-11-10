"""
Decision generators: current stock + recent sales + business rules -> candidate decisions.
Purchasing is decided per brick group by ideal_stock() from the demand forecast (kkj.metrics.ideal_stock); ideals
fixed by hand come back for review through ideal_reviews().

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
from kkj.metrics.sets import PLACE_SEP, unit_word

KIND_WEIGHT = {"understock": 1.0, "transfer": 1.0, "discount": 0.9, "offer": 0.8, "liquidate": 0.7,
               "supplier_slow": 0.6, "pause_buying": 0.6, "supplier_fast": 0.5, "buy_band": 0.5,
               "turn_increase": 0.9, "turn_reduce": 0.7, "turn_phase_out": 0.6, "ideal_review": 0.85, "festival": 0.95,
               "broken_set": 0.8}


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
                        f"Transfer {len(pieces)} {group} from {short(donor)} to {short(receiver)}",
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
                    f"Replenish {group} at {short(receiver)}: {wanted} pieces",
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
    """Deeper for older pieces, from the schedule in the rules (markdown_age_steps / markdown_pct_steps), never above
    the largest discount allowed. Default: 5% up to a year, 10% to two years, 15% after."""
    steps = [float(a) for a in rules["markdown_age_steps"]]
    pcts = [float(p) for p in rules["markdown_pct_steps"]]
    i = sum(age_days > a for a in steps)
    return min(rules["max_offer_discount_pct"], pcts[min(i, len(pcts) - 1)])


def festival_hold(festivals, as_of, rules):
    """(festival names, last festival day) a markdown hold is on for, or None: from markdown_hold_days before a festival
    until festival_days_after it. Overlapping festivals (Dhanteras and Diwali) make one hold that lasts to the last."""
    hold = int(rules.get("markdown_hold_days", 0) or 0)
    if not hold or festivals is None or festivals.empty:
        return None
    as_of = pd.Timestamp(as_of).normalize()
    after = int(rules.get("festival_days_after", 3))
    live = festivals[(festivals["day"] - pd.Timedelta(days=hold) <= as_of)
                     & (as_of <= festivals["day"] + pd.Timedelta(days=after))].sort_values("day")
    if live.empty:
        return None
    names = list(dict.fromkeys(live["name"]))
    return (" and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names), pd.Timestamp(live["day"].max()))


def hold_markdowns(actions, festivals, as_of, rules, existing_keys):
    """Around a festival: drop new markdown recommendations; keep ones already raised, noted as on hold and lower in
    the list (so they don't vanish and come back after the festival)."""
    hold = festival_hold(festivals, as_of, rules)
    if not hold:
        return actions
    name, day = hold
    until = (day + pd.Timedelta(days=int(rules.get("festival_days_after", 3)))).strftime("%d %b")
    kept = []
    for a in actions:
        if a["kind"] != "offer":
            kept.append(a)
        elif a["action_key"] in existing_keys:
            a["summary"] = (f"On hold for {name}: aged pieces often sell at full price around a festival, "
                            f"so consider waiting until after {until}. " + a["summary"])
            a["priority"] = round(a["priority"] * 0.3, 4)
            a["evidence"] = {**a["evidence"], "festival hold": f"{name}; no new markdowns until after {until}"}
            kept.append(a)
    return kept


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
            f"Mark down {len(g)} aged {item} at {short(branch)}",
            f"{len(g)} pieces over {rules['aged_days']} days old in groups that hold more than "
            f"{rules['overstock_days']} days of stock at this showroom. Suggested markdown by age: "
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
            f"Clear {len(g)} pieces over {rules['liquidation_age_days'] // 365} years old at {short(branch)}",
            f"Pieces this old rarely sell. Tag value {money(value)}, recovery value about {money(rec)} "
            f"({100 * rec / value:.0f}%): options: break up and recover, return to the supplier, send to an exhibition, or a final markdown.",
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
                "supplier_slow", name, f"Review supplier {name}: slow sell-through",
                f"Their pieces sell at {s['performance']:.2f}× the company's rate at the same prices "
                f"({100 * s['monthly_sell_rate']:.1f}% a month); {100 * (s['aged_share_now'] or 0):.0f}% of what we hold "
                f"from them is over a year old. Hold back new orders until it moves.",
                held, f"{money(held)} held from this supplier", conf, ev, subject={"supplier": name}))
        elif s["performance"] > 1.5:
            monthly = s["sold_per_month"] * (s["avg_value"] / max(s["avg_pcs"], 1))
            actions.append(_action(
                "supplier_fast", name, f"Increase purchases from {name}",
                f"Their pieces sell at {s['performance']:.2f}× the company's rate at the same prices "
                f"({100 * s['monthly_sell_rate']:.1f}% a month, median {s['median_days_to_sell']:.0f} days to sell).",
                monthly, f"≈{money(monthly)} of sales a month from their pieces", conf, ev, subject={"supplier": name}))
    return actions


# ── buying direction ──────────────────────────────────────────────────────────
# ── weighted stock turn per brick ─────────────────────────────────────────────
def _turn(x):
    return "–" if x is None or pd.isna(x) else "∞" if np.isinf(x) else f"{x:.2f}×"


def _pct(x):
    return "–" if x is None or pd.isna(x) else f"{100 * x:.0f}%"


def ideal_stock(ideal, turns, rules):
    """Increase / reduce / phase out per brick GROUP (company), from the ideal stock (kkj.brain.run.demand_and_ideal):
    company stock against the sum of its branches' ideals. The forecast, its tested accuracy, the service grade and
    each branch's ideal are the evidence; today's turn is shown as a check."""
    min_change = rules["ideal_min_change_pcs"]
    comp, branch, acc = ideal["company"], ideal["branch"], ideal["accuracy"]
    groups = turns[(turns["level"] == "group") & (turns["scope"] == "Company")].set_index("brick")
    product_acc = acc[acc["level"] == "product"].set_index("key") if len(acc) else pd.DataFrame()
    actions = []
    for _, r in comp.iterrows():
        status, change = r["status"], r["change_pcs"]
        g = groups.loc[r["brick_group"]] if r["brick_group"] in groups.index else None
        piece_value = float(g["avg_piece_value"]) if g is not None and pd.notna(g["avg_piece_value"]) else 0.0
        current_value = float(g["current_value"]) if g is not None and pd.notna(g["current_value"]) else 0.0
        u = unit_word(r["item_name"])
        buy = r["item_class_group"] not in rules["no_buy_groups"]
        ideal_pcs = r["ideal_pcs"]
        if status == "increase" and change >= min_change and buy:
            kind, title = "turn_increase", f"Increase {r['brick_group']} to {ideal_pcs:.0f} {u}"
            impact = change * piece_value
            label = f"≈{money(impact)} more stock ({change:+.0f} {u})"
        elif status == "reduce" and -change >= min_change:
            kind, title = "turn_reduce", f"Reduce {r['brick_group']} to {ideal_pcs:.0f} {u}"
            impact = -change * piece_value
            label = f"≈{money(impact)} more than demand needs ({change:+.0f} {u})"
        elif status == "phase_out" and r["current_pcs"] > 0:
            kind, title = "turn_phase_out", f"Stop stocking {r['brick_group']}"
            impact, label = current_value, f"{money(current_value)} held ({r['current_pcs']:.0f} {u}), no demand expected"
        else:
            continue
        rows = branch[branch["brick_group"] == r["brick_group"]]
        selling = rows[rows["ideal_pcs"] > 0]
        why = selling["ideal_from"].value_counts()
        reasons = {"choice": "choice on the counter", "demand": "enough until new stock arrives",
                   "display minimum": "one on display", "override": "a number set by hand"}
        set_by = "; ".join(f"{reasons.get(k, k)} at {n} branch{'es' if n > 1 else ''}" for k, n in why.items())
        summary = (f"Forecast {r['annual']:.1f} {u} over 12 months. Held {r['current_pcs']:.0f}, ideal {ideal_pcs:.0f}"
                   + (f" ({set_by})." if set_by else "."))
        pa = product_acc.loc[r["item_name"]] if r["item_name"] in product_acc.index else None
        evidence = {
            f"held ({u})": float(r["current_pcs"]), f"ideal ({u})": float(ideal_pcs),
            f"forecast, next 12 months ({u})": round(float(r["annual"]), 1),
            "turn today": _turn(g["blended_turn"]) if g is not None else "–",
            f"forecast error, {r['item_name']}": _pct(pa["wape"]) if pa is not None and pd.notna(pa["wape"]) else "–",
        }
        conf = confidence(float(rows["n_window"].fillna(0).sum()))
        actions.append(_action(kind, r["brick_group"], title, summary, impact, label, conf, evidence,
                               subject={"group": r["brick_group"], "item": r["item_name"],
                                        "item_group": r["item_class_group"]}))
    return actions


def ideal_reviews(ideal, overrides, turns, rules):
    """One recommendation per fixed ideal that is due for review (review_on reached): what was fixed and why, what
    the system calculated then and now, and what sold since, to keep, change or remove (kkj.brain.fixes)."""
    if overrides is None or not len(overrides):
        return []
    rows, demand, as_of = ideal["branch"], ideal.get("demand"), pd.Timestamp(ideal.get("as_of"))
    value = turns[(turns["level"] == "group") & (turns["scope"] == "Company")].set_index("brick")["avg_piece_value"]
    col = {"group": "brick_group", "product": "item_name"}
    actions = []
    for o in overrides.itertuples():
        due = pd.Timestamp(o.review_on) if pd.notna(o.review_on) else pd.Timestamp(o.updated_at).tz_localize(None) \
            + pd.Timedelta(days=rules["override_review_days"])
        if due > as_of:
            continue
        hit = rows[(rows[col[o.level]] == o.key) & ((rows["branch"] == o.branch) if o.branch != "*" else True)]
        where = "all branches" if o.branch == "*" else short(o.branch)
        since = pd.Timestamp(o.updated_at).tz_localize(None).normalize()
        days = max((as_of - since).days, 1)
        sold = 0.0
        if demand is not None and len(demand):
            d = demand[(demand[col[o.level]] == o.key) & (demand["sale_date"] > since) & (demand["pcs"] > 0)]
            if o.branch != "*":
                d = d[d["branch"] == o.branch]
            sold = float(d["pcs"].sum())
        expected = float(o.annual_at_fix) * days / 365 if pd.notna(o.annual_at_fix) else None
        calc_now = float(hit["calc_pcs"].sum()) if len(hit) else None
        held = float(hit["current_pcs"].sum()) if len(hit) else 0.0
        unit = float(value.reindex(hit["brick_group"].unique()).astype(float).mean()) if len(hit) else 0.0
        unit = unit if np.isfinite(unit) else 0.0
        gap = abs(o.pcs - (calc_now or 0))
        verdict = ("sold about what the forecast expected" if expected is None or expected == 0 or 0.8 <= sold / expected <= 1.25
                   else "sold MORE than the forecast expected" if sold > expected else "sold LESS than the forecast expected")
        title = f"Review the fixed ideal: {o.key} at {where}"
        summary = (f"Fixed at {o.pcs} on {since:%d %b %Y} by {o.updated_by or 'someone'}: \u201c{o.reason}\u201d. "
                   + (f"The system calculated {float(o.calc_at_fix):.0f} then" if pd.notna(o.calc_at_fix) else "")
                   + (f" and {calc_now:.0f} now. " if calc_now is not None else ". ")
                   + f"In the {days} days since, it sold {sold:.0f}"
                   + (f" against {expected:.1f} forecast ({verdict})" if expected is not None else "")
                   + f"; holding {held:.0f}. Keep the fix (set it again), change it, or remove it to go back to the calculation.")
        evidence = {
            "fixed ideal": f"{o.pcs} ({where})", "reason": o.reason, "fixed by": f"{o.updated_by or ''}, {since:%d %b %Y}",
            "calculated when fixed": f"{float(o.calc_at_fix):.0f}" if pd.notna(o.calc_at_fix) else "–",
            "calculated now": f"{calc_now:.0f}" if calc_now is not None else "–",
            "12-month forecast when fixed": f"{float(o.annual_at_fix):.1f}" if pd.notna(o.annual_at_fix) else "–",
            "12-month forecast now": f"{float(hit['annual'].sum()):.1f}" if len(hit) else "–",
            f"sold in the {days} days since": f"{sold:.0f}",
            "forecast for those days (when fixed)": f"{expected:.1f}" if expected is not None else "–",
            "held when fixed / now": f"{float(o.held_at_fix):.0f} / {held:.0f}" if pd.notna(o.held_at_fix) else f"– / {held:.0f}",
        }
        actions.append(_action("ideal_review", f"{o.level}|{o.key}|{o.branch}", title, summary, gap * unit,
                               f"{o.pcs} fixed vs {calc_now:.0f} calculated" if calc_now is not None else f"{o.pcs} fixed",
                               "medium", evidence,
                               branch=None if o.branch == "*" else o.branch,
                               subject={"group": o.key if o.level == "group" else None,
                                        "item": o.key if o.level == "product" else (hit["item_name"].iloc[0] if len(hit) else None),
                                        "fix_level": o.level, "fix_key": o.key, "fix_branch": o.branch}))
    return actions


# ── broken sets ───────────────────────────────────────────────────────────────
TEMPORARY = ("approval", "transit", "repair")


def broken_sets(broken, rules):
    """One recommendation per broken set (kkj.metrics.sets.broken_sets) worth at least the rule's value.

    A set whose members only sit apart because one is out on approval, in transit or at repair is left alone:
    that is temporary.
    """
    actions = []
    for r in broken.itertuples():
        if r.value_left < rules["broken_set_min_value"]:
            continue
        if r.problem == "members in different places":
            places = r.where.split(PLACE_SEP)
            if any(t in p.lower() for p in places for t in TEMPORARY):
                continue
            title = f"Bring {r.set_type} {r.set_id} together"
            summary = (f"Its pieces are in {len(places)} places ({'; '.join(places)}), so it cannot be shown or sold whole. "
                       f"Move them to one counter.")
            label = f"{money(r.value_left)} set split across places"
        else:
            title = f"Sell what is left of {r.set_type} {r.set_id} on its own"
            summary = (f"{r.missing}. Left in stock: {r.left_in_stock}, {r.age_days:.0f} days old. "
                       f"Relabel and price it as a single piece, or match it with a similar piece to make a set again.")
            label = f"{money(r.value_left)} in an incomplete set"
        evidence = {"set": r.set_id, "set type": r.set_type, "problem": r.problem, "left in stock": r.left_in_stock,
                    "where": r.where, "missing": r.missing or "–",
                    "members": f"{r.members_now} of {r.members_before}"}
        actions.append(_action("broken_set", r.set_id, title, summary, r.value_left, label, "high", evidence,
                               branch=r.branch, subject={"set": r.set_id, "item": r.set_type}))
    return actions


# ── festivals ─────────────────────────────────────────────────────────────────
def festival_stock(outlook, rules):
    """Stock up a product ahead of a festival when it holds less than cover × its expected festival sales.

    outlook: kkj.brain.run.festival_outlook (festival, day, days_away, item_name, group, lift, years,
    past_window_pcs, growth, expected_pcs, held_pcs, held_value). Festivals within 10 days of each other
    (Dhanteras and Diwali) count once per product: the one with the most expected sales.
    """
    cover = rules["festival_stock_cover"]
    o = outlook[(outlook["days_away"] >= 7) & ~outlook["group"].isin(rules["no_buy_groups"])].sort_values("day")
    actions = []
    for item, g in o.groupby("item_name"):
        kept, last_day = [], None
        for r in g.sort_values(["day", "expected_pcs"], ascending=[True, False]).itertuples():
            if last_day is not None and (pd.Timestamp(r.day) - pd.Timestamp(last_day)).days <= 10:
                if r.expected_pcs > kept[-1].expected_pcs:
                    kept[-1] = r
                continue
            kept.append(r)
            last_day = r.day
        for r in kept:
            need = int(np.ceil(cover * r.expected_pcs))
            short = need - r.held_pcs
            if r.expected_pcs < 3 or short <= 0:
                continue
            per_piece = r.held_value / r.held_pcs if r.held_pcs else 0
            impact = short * per_piece
            title = f"Stock up {item} for {r.festival} ({pd.Timestamp(r.day):%d %b})"
            summary = (f"Over the last {r.years} years {item} sold about {r.past_window_pcs:.0f} pieces around "
                       f"{r.festival} ({r.lift:.1f}× a normal day); at this year's pace (×{r.growth:.2f}) expect about "
                       f"{r.expected_pcs:.0f}. Holding {r.held_pcs:.0f}; hold at least {cover:g}× = {need} for choice. "
                       f"{r.days_away} days to go.")
            evidence = {"festival": f"{r.festival}, {pd.Timestamp(r.day):%d %b %Y}", "days away": int(r.days_away),
                        "past festival sales (pcs, average)": round(float(r.past_window_pcs), 1),
                        "years of history": int(r.years), "sales lift around the festival": f"{r.lift:.1f}×",
                        "this year vs average year": f"×{r.growth:.2f}", "expected (pcs)": round(float(r.expected_pcs), 1),
                        "held (pcs)": float(r.held_pcs), "target (pcs)": need}
            conf = "high" if r.years >= 3 and r.expected_pcs >= 10 else "medium" if r.years >= 2 else "low"
            actions.append(_action("festival", f"{item}|{r.festival}|{pd.Timestamp(r.day):%Y-%m-%d}", title, summary,
                                   impact, f"≈{money(impact)} more stock ({short:+.0f} pcs)", conf, evidence,
                                   subject={"item": item, "festival": r.festival, "day": str(r.day)}))
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
            "discount", code, f"Review discounts given by {name}",
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
