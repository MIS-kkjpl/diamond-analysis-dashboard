"""
Ideal stock per brick group and branch, from the demand forecast (kkj.metrics.forecast). Rules: Settings → Ideal
stock; per-group values: Settings → Ideal stock by group (config.stock_param); fixed ideals with a reason:
config.ideal_stock_override (a brick group or a whole product, at a branch or
all branches).

  cover      = replacement time (days, per item group / product / brick group, else the default) + review interval
  demand     = forecast units over the cover (the next months' forecasts, pro rata)
  grade      = A / B / C by the brick group's sales value over the last 12 months (A = top grade_a_pct of selling
               groups, B = the next grade_b_pct, the rest C), unless set for the group
  safety     = enough to meet demand over the cover with the grade's service level, treating demand as a count
               with the spread measured by the accuracy test (dispersion 1 = Poisson; lumpier groups need more).
               Low-volume jewellery sells 0, 1 or 2 pieces in a month, so this is a quantile of a count
               distribution (negative binomial), not "average + z × sigma".
  choice     = customers choose among designs, and holding more of a product sells more of it. At showrooms: hold
               another piece while it adds at least 1 / choice_pcs_per_sale sales a year (the measured sales response
               to pieces held, fc_held_effect; held_choice), with demand taken at that holding, and branches in
               choice_floor_branches never below the months-of-choice ideal. Elsewhere, and for products a branch has
               not held in the last 12 months: choice_months of forecast demand (per item group / product / group)
  ideal      = the largest of demand, choice and the display minimum (for groups the branch sold in the last
               display_min_months), unless an override fixes it (with its reason)
  implied turn = the forecast year's sales ÷ ideal: the check that replaces the ideal-turn target.

The ideal is DECIDED per product at a branch, where the forecast is reliable enough to act on (a brick group at a
branch typically sells one or two pieces a year, so its own forecast is mostly noise). Demand, choice and the
display minimum are added up over the product's brick groups at the branch, the product's ideal is the largest of
them, and it is then split over its brick groups: each keeps its display minimum, the rest goes by forecast (the
mix). Rounding happens once, per product, so small groups no longer each round down to nothing.

Stop stocking (phase_out) needs proof: a group the company held on display for at least stop_display_months of the
last 12 months without a sale. Sales follow what is on display, so a group with an ideal of 0 that has hardly been
stocked is "not proven yet" (unproven) instead.

A brick group's company ideal is the sum of its branches' ideals; stock held anywhere else (Head Office, wholesale)
counts towards the company's stock but has no ideal of its own.
"""
import math

import numpy as np
import pandas as pd

from kkj.metrics import forecast as fcm

GRADES = ("A", "B", "C")
PARAM_LEVELS = {"group": "brick_group", "product": "item_name", "item_group": "item_class_group"}  # most specific first


def count_quantile(mu, dispersion, q):
    """Smallest n with P(demand <= n) >= q, demand a count with mean mu and variance dispersion × mu
    (Poisson when dispersion <= 1, else negative binomial)."""
    if not mu or mu <= 0 or np.isnan(mu):
        return 0
    if dispersion <= 1.0001:
        pmf = math.exp(-mu)
        step = lambda x, p: p * mu / (x + 1)
    else:
        r, p0 = mu / (dispersion - 1.0), 1.0 / dispersion
        pmf = p0 ** r
        step = lambda x, p: p * (x + r) / (x + 1) * (1 - p0)
    cdf, n = pmf, 0
    while cdf < q and n < 10_000:
        pmf = step(n, pmf)
        n += 1
        cdf += pmf
    return n


def cover_demand(fc, cover_days):
    """Forecast units over each row's cover (days), from its monthly forecasts f_1, f_2, ... pro rata."""
    months = np.asarray(cover_days, dtype=float) / 30.4375
    total = np.zeros(len(fc))
    h = 1
    while f"f_{h}" in fc and (months > h - 1).any():
        part = np.clip(months - (h - 1), 0, 1)
        total += fc[f"f_{h}"].to_numpy() * part
        h += 1
    return total


def grades(value_by_group, a_pct, b_pct):
    """{brick group: 'A'|'B'|'C'} by sales value: groups with sales ranked high to low; the top a_pct % are A,
    the next b_pct % B, the rest (and groups without sales) C."""
    v = value_by_group[value_by_group > 0].sort_values(ascending=False)
    n = len(v)
    a_n = max(1, int(round(n * a_pct / 100))) if n and a_pct > 0 else 0   # the best seller is always A
    b_n = int(round(n * b_pct / 100))
    out = {g: "A" if i < a_n else "B" if i < a_n + b_n else "C" for i, g in enumerate(v.index)}
    return {g: out.get(g, "C") for g in value_by_group.index}


def param_lookup(rows, param, params):
    """The most specific stock_param value for each row (brick group > product > item group), else None.
    params: config.stock_param rows (param, level, key, value)."""
    out = pd.Series([None] * len(rows), index=rows.index, dtype=object)
    p = params[params["param"] == param] if len(params) else params
    for level, col in PARAM_LEVELS.items():
        table = dict(zip(p.loc[p["level"] == level, "key"], p.loc[p["level"] == level, "value"])) if len(p) else {}
        if table:
            hit = rows[col].map(table)
            out = out.where(out.notna(), hit)
    return out


def compute(fc, current, value_by_group, sold_recently, rules, params, overrides, dispersion, exposure=None,
            held_attrs=None, held=None, showrooms=None):
    """Ideal stock per (brick group, branch).

    fc:             kkj.metrics.forecast.forecast_at (index brick_group, branch; f_1.., annual, attrs)
    current:        units held now per (brick_group, branch) (Series)
    value_by_group: sales value over the last 12 months per brick group (Series), for grades
    sold_recently:  set of (brick_group, branch) that sold within display_min_months
    params:         config.stock_param rows; overrides: config.ideal_stock_override rows (brick_group, branch, pcs, reason)
    dispersion:     ({item group: dispersion}, overall default) from kkj.metrics.forecast.dispersion
    exposure:       per brick group (company): display_months (of the last 12 with stock anywhere) and sold_12m
    held_attrs:     item group / purity / product per (brick_group, branch) for groups held but never forecast
    held:           average pieces held per (item_name, branch) over the last 12 months (forecast.product_held)
    showrooms:      branches where choice follows the sales response to pieces held (decide_by_product)
    """
    by_group, default_disp = dispersion
    df = fc.copy()
    keys = df.index.union(current.index) if len(current) else df.index
    df = df.reindex(keys)
    df["current_pcs"] = current.reindex(keys).fillna(0).to_numpy() if len(current) else 0.0
    rows = df.reset_index()
    if held_attrs is not None and len(held_attrs):
        look = pd.MultiIndex.from_frame(rows[["brick_group", "branch"]])
        for c in ("item_class_group", "purity", "item_name"):
            if c in held_attrs:
                fill = held_attrs[c].reindex(look).to_numpy()
                rows[c] = rows[c].where(rows[c].notna(), fill) if c in rows else fill
    fcols = [c for c in rows if c.startswith("f_")]
    rows[fcols + ["rate", "annual"]] = rows[fcols + ["rate", "annual"]].fillna(0)

    repl = pd.to_numeric(param_lookup(rows, "replacement_days", params), errors="coerce")
    rows["replacement_days"] = repl.fillna(rules["replacement_days"])
    rows["replacement_from"] = np.where(repl.notna(), "set for this group", "default")
    rows["cover_days"] = rows["replacement_days"] + rules["review_days"]
    rows["cover_demand"] = cover_demand(rows, rows["cover_days"])

    g = grades(value_by_group, rules["grade_a_pct"], rules["grade_b_pct"]) if len(value_by_group) else {}
    set_grade = param_lookup(rows, "grade", params)
    rows["grade"] = set_grade.where(set_grade.isin(GRADES), rows["brick_group"].map(g).fillna("C"))
    rows["grade_from"] = np.where(set_grade.isin(GRADES), "set for this group", "sales value")
    service = {"A": rules["service_a_pct"], "B": rules["service_b_pct"], "C": rules["service_c_pct"]}
    rows["service_pct"] = rows["grade"].map(service)
    rows["dispersion"] = rows["item_class_group"].map(by_group).fillna(default_disp)
    rows["for_demand"] = [count_quantile(m, d, s / 100) for m, d, s in
                          zip(rows["cover_demand"], rows["dispersion"], rows["service_pct"])]

    dmin = pd.to_numeric(param_lookup(rows, "display_min", params), errors="coerce").fillna(rules["display_min_pcs"])
    recent = pd.Series([(a, b) in sold_recently for a, b in zip(rows["brick_group"], rows["branch"])], index=rows.index)
    rows["display_min"] = np.where(recent, dmin, 0)
    choice = pd.to_numeric(param_lookup(rows, "choice_months", params), errors="coerce").fillna(rules["choice_months"])
    rows["choice_months"] = choice
    decide_by_product(rows, rules, held, showrooms)
    rows["calc_from"] = rows["ideal_from"]

    apply_overrides(rows, overrides)

    ex = exposure if exposure is not None else pd.DataFrame(columns=["display_months", "sold_12m"])
    rows["display_months"] = ex["display_months"].reindex(rows["brick_group"]).to_numpy(dtype=float)
    rows["sold_12m"] = ex["sold_12m"].reindex(rows["brick_group"]).to_numpy(dtype=float)
    rows["change_pcs"] = rows["ideal_pcs"] - rows["current_pcs"]
    rows["implied_turn"] = np.where(rows["ideal_pcs"] > 0,
                                    rows["annual_at_ideal"] / rows["ideal_pcs"].where(rows["ideal_pcs"] > 0), np.nan)
    rows["status"] = status(rows["current_pcs"], rows["ideal_pcs"], rules, proven_dead(rows, rules))
    return rows


def held_choice(F, x0, beta, pcs_per_sale, cap):
    """Pieces to hold for choice from the sales response to pieces held: S(x) = F * ((x + c) / (x0 + c)) ** beta, F
    the forecast at what was held (x0). One more piece is held while it adds at least 1 / pcs_per_sale sales a year:
    x = (beta * F / (m * (x0 + c) ** beta)) ** (1 / (1 - beta)) - c, at most twice x0 (or `cap`, the months-of-choice
    figure, if higher). Where nothing was held there is no basis: `cap`."""
    F, x0, cap = (np.asarray(a, dtype=float) for a in (F, x0, cap))
    m, c = 1.0 / pcs_per_sale, fcm.HELD_C
    x = (beta * F / (m * (x0 + c) ** beta)) ** (1 / (1 - beta)) - c
    x = np.minimum(np.maximum(x, 0), np.maximum(fcm.HELD_RANGE * x0, cap))
    return np.round(np.where(x0 > 0, x, cap))


def decide_by_product(rows, rules=None, held=None, showrooms=None):
    """In place: the ideal per product at a branch (largest of demand, choice and display minimum, each added up over
    the product's brick groups), split over its brick groups: each keeps its display minimum, the rest by forecast.
    for_demand / for_choice become the product's figures at the branch (the same on each of its rows), mix_share the
    row's share of the product's forecast, product_ideal the product's ideal at the branch.

    With held (average pieces held per (item_name, branch) over the last 12 months), showrooms and a sales response
    (rules fc_held_effect): at showrooms, choice comes from held_choice, demand is the forecast at the new holding,
    and branches in choice_floor_branches never go below the months-of-choice ideal. annual_at_ideal = the forecast
    at the ideal holding."""
    r = rows.assign(_name=rows["item_name"].fillna("(unknown)"))
    r["_choice"] = r["annual"] * r["choice_months"] / 12
    r["_svc"] = r["service_pct"] * r["annual"]
    r["_disp"] = r["dispersion"] * r["cover_demand"]
    g = r.groupby(["_name", "branch"])
    p = pd.DataFrame({"cover": g["cover_demand"].sum(), "annual": g["annual"].sum(), "choice": g["_choice"].sum(),
                      "display": g["display_min"].sum(), "svc": g["_svc"].sum(), "svc_max": g["service_pct"].max(),
                      "disp": g["_disp"].sum(), "disp_mean": g["dispersion"].mean()})
    p["service"] = np.where(p["annual"] > 0, p["svc"] / p["annual"].where(p["annual"] > 0), p["svc_max"])
    p["dispersion"] = np.where(p["cover"] > 0, p["disp"] / p["cover"].where(p["cover"] > 0), p["disp_mean"])
    p["for_demand"] = [count_quantile(m, d, s_ / 100) for m, d, s_ in zip(p["cover"], p["dispersion"], p["service"])]
    p["for_choice"] = np.round(p["choice"])
    parts = p[["for_demand", "display", "for_choice"]].astype(float)
    p["ideal"] = parts.max(axis=1)
    p["from"] = parts.idxmax(axis=1).map({"for_demand": "demand", "display": "display minimum", "for_choice": "choice"})
    beta = float(rules.get("fc_held_effect", 0) or 0) if rules else 0.0
    p["held_12m"] = held.reindex(p.index).fillna(0).to_numpy(dtype=float) if held is not None else 0.0
    p["annual_at_ideal"] = p["annual"]
    if beta > 0 and held is not None and showrooms is not None:
        shop = p.index.get_level_values("branch").isin(list(showrooms)) & (p["held_12m"] > 0)
        months_ideal = p["ideal"].copy()
        choice = held_choice(p["annual"], p["held_12m"], beta, rules["choice_pcs_per_sale"], p["for_choice"])
        resp = fcm.held_response(np.maximum(choice, p["for_demand"]), p["held_12m"], beta)
        demand = np.array([count_quantile(m, d, s_ / 100) for m, d, s_ in
                           zip(p["cover"] * resp, p["dispersion"], p["service"])])
        new = np.c_[demand, p["display"], choice]
        new_ideal = new.max(axis=1)
        floor = p.index.get_level_values("branch").isin(list(rules.get("choice_floor_branches") or []))
        new_ideal = np.where(floor, np.maximum(new_ideal, months_ideal), new_ideal)
        new_from = np.array(["demand", "display minimum", "choice"])[new.argmax(axis=1)]
        new_from = np.where(floor & (months_ideal > new.max(axis=1)), p["from"], new_from)
        p["for_demand"] = np.where(shop, demand, p["for_demand"])
        p["for_choice"] = np.where(shop, choice, p["for_choice"])
        p["ideal"] = np.where(shop, new_ideal, p["ideal"])
        p["from"] = np.where(shop, new_from, p["from"])
        p["annual_at_ideal"] = p["annual"] * np.where(shop, fcm.held_response(p["ideal"], p["held_12m"], beta), 1.0)
    p.loc[p["ideal"] == 0, "from"] = "no demand"

    ideal = pd.Series(0.0, index=r.index)
    for key, idx in g.groups.items():
        sub = r.loc[idx]
        base = sub["display_min"].to_numpy(dtype=float)
        rest = max(0.0, float(p.at[key, "ideal"]) - base.sum())
        share = split(rest, sub["annual"].to_numpy(dtype=float)) if rest > 0 else np.zeros(len(sub))
        ideal.loc[idx] = base + share
    look = pd.MultiIndex.from_arrays([r["_name"], r["branch"]])
    rows["for_demand"] = p["for_demand"].reindex(look).to_numpy()
    rows["for_choice"] = p["for_choice"].reindex(look).to_numpy()
    rows["product_ideal"] = p["ideal"].reindex(look).to_numpy()
    rows["held_12m"] = p["held_12m"].reindex(look).to_numpy()
    tot = p["annual"].reindex(look).to_numpy()
    rows["mix_share"] = np.where(tot > 0, rows["annual"].to_numpy() / np.where(tot > 0, tot, 1), np.nan)
    resp = (p["annual_at_ideal"] / p["annual"].where(p["annual"] > 0)).fillna(1.0).reindex(look).to_numpy()
    rows["annual_at_ideal"] = rows["annual"].to_numpy() * resp
    rows["ideal_pcs"] = ideal.to_numpy()
    rows["ideal_from"] = np.where(rows["ideal_pcs"] > 0, p["from"].reindex(look).to_numpy(), "no demand")


def proven_dead(rows, rules):
    """Per row: the group was held on display (anywhere in the company) for at least stop_display_months of the last
    12 months and sold nothing in them, so stopping it is backed by evidence."""
    n = len(rows)
    months = pd.to_numeric(rows["display_months"], errors="coerce").fillna(0).to_numpy() if "display_months" in rows else np.zeros(n)
    sold = pd.to_numeric(rows["sold_12m"], errors="coerce").fillna(0).to_numpy() if "sold_12m" in rows else np.zeros(n)
    return (months >= rules["stop_display_months"]) & (sold <= 0)


def split(total, weights):
    """Whole pieces adding up to total, in proportion to weights (largest remainder); evenly when all are 0."""
    w = np.asarray(weights, dtype=float)
    w = np.where(np.isfinite(w) & (w > 0), w, 0.0)
    if w.sum() == 0:
        w = np.ones(len(w))
    exact = total * w / w.sum()
    out = np.floor(exact)
    left = int(round(total - out.sum()))
    if left > 0:
        out[np.argsort(-(exact - out))[:left]] += 1
    return out


def apply_overrides(rows, overrides):
    """Fixed ideals (config.ideal_stock_override) onto the rows, in place. A product's or an all-branch ('*') number is
    split over the matching (brick group, branch) rows in proportion to their calculated ideals. The more specific wins:
    product at all branches, then product at a branch, then brick group at all branches, then brick group at a branch."""
    rows["override_pcs"], rows["override_reason"] = np.nan, None
    rows["calc_pcs"] = rows["ideal_pcs"].astype(float)            # what the system calculated, before any fix
    if overrides is None or not len(overrides):
        return
    col = {"product": "item_name", "group": "brick_group"}
    order = {("product", True): 0, ("product", False): 1, ("group", True): 2, ("group", False): 3}
    o = overrides.assign(_o=[order[(lv, br == "*")] for lv, br in zip(overrides["level"], overrides["branch"])])
    calculated = rows["ideal_pcs"].copy()
    for r in o.sort_values("_o").itertuples():
        hit = (rows[col[r.level]] == r.key) & ((rows["branch"] == r.branch) if r.branch != "*" else True)
        if not hit.any():
            continue
        rows.loc[hit, "override_pcs"] = split(r.pcs, calculated[hit])
        rows.loc[hit, "override_reason"] = r.reason
    fixed = rows["override_pcs"].notna()
    rows.loc[fixed, "ideal_pcs"] = rows.loc[fixed, "override_pcs"]
    rows.loc[fixed, "ideal_from"] = "override"


def status(current, ideal, rules, proven=None):
    """keep / increase / reduce: outside the ideal ± ideal_band_pct AND at least ideal_min_change_pcs. Held with an
    ideal of 0: phase_out when proven (proven_dead), else unproven. proven None = treat as proven."""
    current, ideal = np.asarray(current, dtype=float), np.asarray(ideal, dtype=float)
    proven = np.ones(len(current), dtype=bool) if proven is None else np.asarray(proven, dtype=bool)
    band = rules["ideal_band_pct"] / 100
    need = np.maximum(rules["ideal_min_change_pcs"], band * ideal)
    change = ideal - current
    dead = (ideal == 0) & (current > 0)
    return np.select([dead & proven, dead, change >= need, -change >= need],
                     ["phase_out", "unproven", "increase", "reduce"], "keep")


def by_product(rows, rules, keys=("item_name",)):
    """Rows (brick groups) added up per product (and any other keys): held, ideal, calculated ideal, forecast and the
    status. A product with an ideal of 0 is phase_out only when every held group in it is proven dead."""
    keys = list(keys)
    r = rows.copy()
    for c in ("current_pcs", "ideal_pcs", "calc_pcs", "annual"):
        r[c] = pd.to_numeric(r[c], errors="coerce").astype(float)
    r["calc_pcs"] = r["calc_pcs"].fillna(r["ideal_pcs"])
    r["_dead"] = proven_dead(r, rules) | (r["current_pcs"].fillna(0) <= 0)
    g = r.groupby(keys, dropna=False).agg(item_class_group=("item_class_group", "first"), current_pcs=("current_pcs", "sum"),
                                          ideal_pcs=("ideal_pcs", "sum"), calc_pcs=("calc_pcs", "sum"),
                                          annual=("annual", "sum"), groups=("brick_group", "nunique"),
                                          all_dead=("_dead", "all"))
    g["change_pcs"] = g["ideal_pcs"] - g["current_pcs"]
    g["status"] = status(g["current_pcs"], g["ideal_pcs"], rules, g["all_dead"])
    return g.reset_index()


def company(rows, rules):
    """One row per brick group: company stock (every branch), the sum of the branches' ideals, forecast and the
    status; branch rows stay as the evidence."""
    agg = rows.groupby("brick_group").agg(
        item_class_group=("item_class_group", "first"), purity=("purity", "first"), item_name=("item_name", "first"),
        current_pcs=("current_pcs", "sum"), ideal_pcs=("ideal_pcs", "sum"), calc_pcs=("calc_pcs", "sum"),
        annual=("annual", "sum"), annual_at_ideal=("annual_at_ideal", "sum"),
        cover_demand=("cover_demand", "sum"), grade=("grade", "first"), service_pct=("service_pct", "first"),
        cover_days=("cover_days", "max"), overrides=("override_pcs", lambda s: int(s.notna().sum())),
        display_months=("display_months", "first"), sold_12m=("sold_12m", "first"))
    agg["change_pcs"] = agg["ideal_pcs"] - agg["current_pcs"]
    agg["implied_turn"] = np.where(agg["ideal_pcs"] > 0,
                                   agg["annual_at_ideal"] / agg["ideal_pcs"].where(agg["ideal_pcs"] > 0), np.nan)
    agg["status"] = status(agg["current_pcs"], agg["ideal_pcs"], rules, proven_dead(agg, rules))
    return agg.reset_index()
