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
  choice     = choice_months of forecast demand: customers choose among designs, and holding more of a group
               sells more of it, so a branch holds at least this much (per item group / product / brick group)
  ideal      = the largest of demand, choice and the display minimum (for groups the branch sold in the last
               display_min_months), unless an override fixes it (with its reason)
  implied turn = the forecast year's sales ÷ ideal: the check that replaces the ideal-turn target.

A brick group's company ideal is the sum of its branches' ideals; stock held anywhere else (Head Office, wholesale)
counts towards the company's stock but has no ideal of its own.
"""
import math

import numpy as np
import pandas as pd

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


def compute(fc, current, value_by_group, sold_recently, rules, params, overrides, dispersion):
    """Ideal stock per (brick group, branch).

    fc:             kkj.metrics.forecast.forecast_at (index brick_group, branch; f_1.., annual, attrs)
    current:        units held now per (brick_group, branch) (Series)
    value_by_group: sales value over the last 12 months per brick group (Series), for grades
    sold_recently:  set of (brick_group, branch) that sold within display_min_months
    params:         config.stock_param rows; overrides: config.ideal_stock_override rows (brick_group, branch, pcs, reason)
    dispersion:     ({item group: dispersion}, overall default) from kkj.metrics.forecast.dispersion
    """
    by_group, default_disp = dispersion
    df = fc.copy()
    keys = df.index.union(current.index) if len(current) else df.index
    df = df.reindex(keys)
    df["current_pcs"] = current.reindex(keys).fillna(0).to_numpy() if len(current) else 0.0
    rows = df.reset_index()
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
    rows["for_choice"] = np.round(rows["annual"] * choice / 12)
    parts = rows[["for_demand", "display_min", "for_choice"]].astype(float)
    rows["ideal_pcs"] = parts.max(axis=1)
    rows["ideal_from"] = parts.idxmax(axis=1).map({"for_demand": "demand", "display_min": "display minimum",
                                                   "for_choice": "choice"})
    rows.loc[rows["ideal_pcs"] == 0, "ideal_from"] = "no demand"

    apply_overrides(rows, overrides)

    rows["change_pcs"] = rows["ideal_pcs"] - rows["current_pcs"]
    rows["implied_turn"] = np.where(rows["ideal_pcs"] > 0, rows["annual"] / rows["ideal_pcs"].where(rows["ideal_pcs"] > 0), np.nan)
    rows["status"] = status(rows["current_pcs"], rows["ideal_pcs"], rules)
    return rows


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


def status(current, ideal, rules):
    """keep / increase / reduce / phase_out: outside the ideal ± ideal_band_pct AND at least turn_min_change_pcs."""
    current, ideal = np.asarray(current, dtype=float), np.asarray(ideal, dtype=float)
    band = rules["ideal_band_pct"] / 100
    need = np.maximum(rules["turn_min_change_pcs"], band * ideal)
    change = ideal - current
    return np.select([(ideal == 0) & (current > 0), change >= need, -change >= need],
                     ["phase_out", "increase", "reduce"], "keep")


def company(rows, rules):
    """One row per brick group: company stock (every branch), the sum of the branches' ideals, forecast and the
    status; branch rows stay as the evidence."""
    agg = rows.groupby("brick_group").agg(
        item_class_group=("item_class_group", "first"), purity=("purity", "first"), item_name=("item_name", "first"),
        current_pcs=("current_pcs", "sum"), ideal_pcs=("ideal_pcs", "sum"), annual=("annual", "sum"),
        cover_demand=("cover_demand", "sum"), grade=("grade", "first"), service_pct=("service_pct", "first"),
        cover_days=("cover_days", "max"), overrides=("override_pcs", lambda s: int(s.notna().sum())))
    agg["change_pcs"] = agg["ideal_pcs"] - agg["current_pcs"]
    agg["implied_turn"] = np.where(agg["ideal_pcs"] > 0, agg["annual"] / agg["ideal_pcs"].where(agg["ideal_pcs"] > 0), np.nan)
    agg["status"] = status(agg["current_pcs"], agg["ideal_pcs"], rules)
    return agg.reset_index()
