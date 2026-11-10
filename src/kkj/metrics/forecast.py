"""
Demand forecast per brick group and branch, in selling units a month (kkj.metrics.forecast).

What is forecast is demand the STOCK has to meet: net pieces sold, leaving out sales of pieces booked against a
customer order (custom work; rule fc_exclude_orders). All rules named fc_* are in Settings → Demand forecast.

1. Monthly panel. For every brick group at every branch and month: pieces sold (never below 0 in a month), and
   availability = the share of that month's stock snapshots in which the group was in stock at the branch (a
   month-end snapshot also stands for the opening of the next month, so years with month-end stock only still
   give two readings a month).

2. Own selling rate over the last fc_level_months, in an average month: each month's sales are divided by its
   seasonal index (kkj.metrics.seasonality: product pattern, else item group, else none) and, with the stock-out
   correction on, by its availability: a group in stock half the month and selling 2 sold at a rate of 4.
   Availability is measured in the whole company by default (fc_availability_basis): branches sell pieces
   brought in from Head Office or another branch, so a group missing from one counter was still on offer.
   Months in stock less than fc_min_availability_pct with no sales say nothing about demand and are skipped;
   availability is never taken below that floor, so one lucky sale cannot explode the rate. Recent months weigh
   more: a month's weight halves every fc_half_life_months.

3. Borrowing. Few groups sell often enough to stand on their own (the median brick sells about once a year),
   so each level leans on the one above it, from the top down:
       item group (company) → item group at the branch → product at the branch → purity · product at the branch
       → brick group at the branch
   A level's prior is its parent's rate times its share of the parent's sales over all earlier history; its
   rate = (e × own rate + k × prior) / (e + k), with e = the (recency-weighted) months it was observed in stock
   and k = fc_shrink_months: months of zero sales are evidence too, so a dead group is not propped up by its
   parent, and a new one leans on it.
   A group with no history at all gets its parent's rate times an even share.

4. Trend. Pieces sold in the last 12 months against the 12 before, at product × branch, pulled towards the item
   group × branch trend (fc_trend_pseudo_pcs pseudo-pieces) and capped at ± fc_trend_cap_pct. Off with fc_trend.

5. Forecast for month h after the origin = rate × seasonal index of that calendar month × trend.

backtest() replays this as of past month-ends, using only what was known then, against what actually sold and
two simple methods (the same months last year; the last 12 months' average). Its spread of errors also sizes
the safety margin in kkj.metrics.ideal_stock.
"""
import numpy as np
import pandas as pd

from kkj.metrics import seasonality as sn

BASE = ["brick_group", "branch"]
# borrowing ladder, finest first: (name, keys)
LADDER = [("group", ["brick_group", "branch"]),
          ("purity_product", ["purity", "item_name", "branch"]),
          ("product", ["item_name", "branch"]),
          ("item_group", ["item_class_group", "branch"]),
          ("item_group_all", ["item_class_group"])]
ATTRS = ["item_class_group", "purity", "item_name"]
HORIZON = 12


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


# ── 1. monthly panel ──────────────────────────────────────────────────────────
def panel(stock, sales, rules, last_month):
    """(panel, attrs). panel: one row per (brick group, branch, month) with sold, avail (NaN = no snapshot that
    month), held (average units). attrs: item group, purity and product per (brick group, branch).

    stock: add_brick rows (snapshot_date, branch, brick_group, attrs, pcs), any set of snapshot dates.
    sales: add_brick net sale rows (sale_date, branch, brick_group, attrs, pcs, order_booked).
    """
    last_month = pd.Period(last_month, "M")
    s = sales
    if rules["fc_exclude_orders"] and "order_booked" in s:
        s = s[~s["order_booked"].fillna(False).astype(bool)]
    s = s.assign(month=pd.to_datetime(s["sale_date"]).dt.to_period("M"))
    s = s[s["month"] <= last_month]
    sold = s.groupby(BASE + ["month"])["pcs"].sum()

    st = stock.assign(d=pd.to_datetime(stock["snapshot_date"]))
    dates = pd.DataFrame({"d": pd.Series(st["d"].unique())})
    dates["month"] = dates["d"].dt.to_period("M")
    opening = dates[dates["d"].dt.is_month_end].assign(month=lambda x: x["month"] + 1)
    dm = pd.concat([dates, opening], ignore_index=True)
    dm = dm[dm["month"] <= last_month]
    n_snap = dm.groupby("month").size()
    by_date = st.groupby(BASE + ["d"])["pcs"].sum().reset_index()
    by_date = by_date[by_date["pcs"] > 0].merge(dm, on="d")
    held = by_date.groupby(BASE + ["month"]).agg(in_stock=("pcs", "size"), held=("pcs", "sum"))
    anywhere = by_date.drop_duplicates(["brick_group", "d", "month"]).groupby(["brick_group", "month"]).size()

    keys = sold.index.droplevel("month").union(held.index.droplevel("month")).unique()
    first = min(s["month"].min() if len(s) else last_month, dm["month"].min() if len(dm) else last_month)
    months = pd.period_range(first, last_month, freq="M")
    idx = pd.MultiIndex.from_tuples([k + (m,) for k in keys for m in months], names=BASE + ["month"])
    p = pd.DataFrame(index=idx)
    p["sold"] = sold.reindex(idx).fillna(0).clip(lower=0)
    snaps = n_snap.reindex(p.index.get_level_values("month")).to_numpy(dtype=float)
    p["avail"] = held["in_stock"].reindex(idx).fillna(0).to_numpy() / snaps          # NaN where no snapshot
    p["held"] = held["held"].reindex(idx).fillna(0).to_numpy() / snaps
    # in stock anywhere in the company (a branch can sell a piece brought in from Head Office or another branch)
    company = pd.MultiIndex.from_arrays([p.index.get_level_values("brick_group"), p.index.get_level_values("month")])
    p["avail_company"] = anywhere.reindex(company).fillna(0).to_numpy() / snaps

    attrs = pd.concat([stock[BASE + ATTRS], sales[BASE + ATTRS]]).drop_duplicates(BASE).set_index(BASE)
    attrs = attrs.reindex(keys)
    return p.reset_index(), attrs


# ── seasonal factors ──────────────────────────────────────────────────────────
def season_tables(sales, rules, until=None):
    """(product index, item group index): DataFrames index=key, columns 1..12 (calendar months), from sales up to
    `until` (order-booked left out like the panel). Empty when there is not a complete financial year."""
    s = sales
    if rules["fc_exclude_orders"] and "order_booked" in s:
        s = s[~s["order_booked"].fillna(False).astype(bool)]
    if until is not None:
        s = s[pd.to_datetime(s["sale_date"]) <= pd.Timestamp(until)]
    if not rules["fc_seasonal"] or s.empty:
        return pd.DataFrame(), pd.DataFrame()
    out = []
    for key in ("item_name", "item_class_group"):
        idx = sn.monthly_index(s[["sale_date", key, "pcs"]], key, rules["season_min_year_pcs"])
        cols = {f"index_{m}": m for m in range(1, 13)}
        out.append(idx.set_index(key)[list(cols)].rename(columns=cols) if len(idx) else pd.DataFrame())
    if rules["fc_season_level"] == "item_group":            # products' own patterns are noisy: group pattern only
        out[0] = pd.DataFrame()
    return tuple(out)


def season_factor(names, groups, moy, tables):
    """Seasonal index per row: the product's, else its item group's, else 1. names/groups/moy: aligned arrays."""
    prod, grp = tables
    f = np.full(len(moy), np.nan)
    for table, keys in ((prod, names), (grp, groups)):
        if table is None or table.empty:
            continue
        stacked = table.stack()
        look = pd.MultiIndex.from_arrays([np.asarray(keys), np.asarray(moy)])
        vals = stacked.reindex(look).to_numpy(dtype=float)
        f = np.where(np.isnan(f), vals, f)
    f = np.where(np.isnan(f) | (f <= 0), 1.0, f)
    return f


# ── 2-5. forecast at one origin ───────────────────────────────────────────────
def forecast_at(p, attrs, origin, rules, tables, horizon=HORIZON):
    """Forecast for every (brick group, branch) as of the end of month `origin`, from panel rows up to it.

    Returns a DataFrame indexed by BASE: rate (units in an average month), trend, f_1 .. f_<horizon> (units per
    month ahead), plus own_rate, prior, n_window (units sold in the window) and level-by-level evidence.
    """
    origin = pd.Period(origin, "M")
    L, half = int(rules["fc_level_months"]), float(rules["fc_half_life_months"])
    floor = rules["fc_min_availability_pct"] / 100
    k = float(rules["fc_shrink_months"])
    hist = p[p["month"] <= origin]
    win = hist[hist["month"] > origin - L].copy()
    if win.empty:
        return pd.DataFrame()
    a = attrs.reindex(pd.MultiIndex.from_frame(win[BASE])).reset_index(drop=True)
    win = pd.concat([win.reset_index(drop=True), a], axis=1)
    age = (origin - win["month"]).apply(lambda x: x.n).to_numpy()
    win["w"] = 0.5 ** (age / half)
    moy = win["month"].dt.month.to_numpy()
    win["sf_prod"] = season_factor(win["item_name"], win["item_class_group"], moy, tables)
    win["sf_grp"] = season_factor(np.full(len(win), None), win["item_class_group"], moy, tables)

    # finest level: availability-corrected
    basis = "avail_company" if rules["fc_availability_basis"] == "company" else "avail"
    avail = win[basis].fillna(1.0)
    if rules["fc_stockout_correction"]:
        valid = (avail >= floor) | (win["sold"] > 0)
        adj = win["sold"] / win["sf_prod"] / np.maximum(avail, floor)
    else:
        valid = pd.Series(True, index=win.index)
        adj = win["sold"] / win["sf_prod"]
    win["vw"] = win["w"] * valid
    win["x"] = adj * valid
    g0 = win.groupby(BASE)
    own0 = (win["x"] * win["w"]).groupby([win[c] for c in BASE]).sum() / g0["vw"].sum().replace(0, np.nan)
    n0 = (win["sold"] * valid).groupby([win[c] for c in BASE]).sum()
    e0 = g0["vw"].sum()                                   # exposure: weighted months observed in stock

    # shares over all history up to the origin (availability-corrected at the finest level)
    h = hist.copy()
    ha = h[basis].fillna(1.0)
    hvalid = (ha >= floor) | (h["sold"] > 0) if rules["fc_stockout_correction"] else pd.Series(True, index=h.index)
    h["adj_total"] = np.where(hvalid, h["sold"] / np.maximum(ha, floor) if rules["fc_stockout_correction"]
                              else h["sold"], 0.0)
    totals0 = h.groupby(BASE)["adj_total"].sum()
    keys0 = attrs.reindex(totals0.index).reset_index()
    keys0["total"] = totals0.to_numpy()

    # upper levels: own rates from raw (deseasonalised) sales
    rates, evidence = {}, {}
    for name, cols in LADDER[1:]:
        sf = win["sf_grp"] if name.startswith("item_group") else win["sf_prod"]
        monthly = win.assign(x=win["sold"] / sf).groupby(cols + ["month"]).agg(x=("x", "sum"), sold=("sold", "sum"),
                                                                              w=("w", "first"))
        lv = list(range(len(cols)))                         # the level's keys: one series per key, one row a month
        own = (monthly["x"] * monthly["w"]).groupby(level=lv).sum() / monthly["w"].groupby(level=lv).sum()
        rates[name] = (own, monthly["sold"].groupby(level=lv).sum(), monthly["w"].groupby(level=lv).sum())

    # top-down shrinkage
    top_name, top_cols = LADDER[-1]
    final = {top_name: rates[top_name][0]}
    for (child, ccols), (parent, pcols) in zip(reversed(LADDER[:-1]), reversed(LADDER[1:])):
        own, n, e = (own0, n0, e0) if child == "group" else rates[child]
        both = list(dict.fromkeys(ccols + pcols))           # the child's keys plus its parent's
        ctab = keys0.groupby(both, dropna=False)["total"].sum().reset_index()
        parent_total = ctab.groupby(pcols, dropna=False)["total"].transform("sum")
        siblings = ctab.groupby(pcols, dropna=False)["total"].transform("size")
        share = (ctab["total"] + 1.0 / siblings) / (parent_total + 1.0)
        prior_rate = final[parent].reindex(pd.MultiIndex.from_frame(ctab[pcols]) if len(pcols) > 1
                                           else pd.Index(ctab[pcols[0]])).to_numpy()
        prior = pd.Series(np.nan_to_num(prior_rate) * share.to_numpy(),
                          index=pd.MultiIndex.from_frame(ctab[ccols]) if len(ccols) > 1 else pd.Index(ctab[ccols[0]]))
        prior = prior.groupby(level=list(range(len(ccols)))).sum()   # a child under two parents: both shares
        own_r = own.reindex(prior.index)
        n_r = n.reindex(prior.index).fillna(0)
        e_r = e.reindex(prior.index).fillna(0)
        # Gamma-Poisson posterior mean: the prior counts as k months at the parent-implied rate
        rate = ((e_r * own_r.fillna(0) + k * prior) / (e_r + k)).where(own_r.notna(), prior)
        final[child] = rate
        if child == "group":
            evidence = {"own_rate": own_r, "prior": prior, "n_window": n_r, "exposure": e_r}

    out = pd.DataFrame({"rate": final["group"]})
    for c, v in evidence.items():
        out[c] = v.reindex(out.index)
    out = out.join(attrs, how="left")

    # trend at product × branch, pulled towards item group × branch
    out["trend"] = 1.0
    if rules["fc_trend"] and len(hist["month"].unique()) >= 24:
        c = float(rules["fc_trend_pseudo_pcs"])
        cap = rules["fc_trend_cap_pct"] / 100
        recent = hist[hist["month"] > origin - 12]
        prev = hist[(hist["month"] <= origin - 12) & (hist["month"] > origin - 24)]
        def totals(df, cols):
            j = df.merge(attrs.reset_index(), on=BASE, how="left")
            return j.groupby(cols)["sold"].sum()
        sg, pg = totals(recent, ["item_class_group", "branch"]), totals(prev, ["item_class_group", "branch"])
        tg = ((sg.reindex(sg.index.union(pg.index)).fillna(0) + c) / (pg.reindex(sg.index.union(pg.index)).fillna(0) + c))
        sp, pp = totals(recent, ["item_name", "branch"]), totals(prev, ["item_name", "branch"])
        ix = sp.index.union(pp.index)
        prod_grp = attrs.reset_index().drop_duplicates(["item_name", "branch"]).set_index(["item_name", "branch"])["item_class_group"]
        tg_for_p = tg.reindex(pd.MultiIndex.from_arrays([prod_grp.reindex(ix).to_numpy(), ix.get_level_values(1)])).to_numpy()
        tg_for_p = np.where(np.isnan(tg_for_p), 1.0, tg_for_p)
        tp = (sp.reindex(ix).fillna(0).to_numpy() + c * tg_for_p) / (pp.reindex(ix).fillna(0).to_numpy() + c)
        tp = pd.Series(np.clip(tp, 1 / (1 + cap), 1 + cap), index=ix)
        look = pd.MultiIndex.from_arrays([out["item_name"].to_numpy(), out.index.get_level_values("branch")])
        out["trend"] = np.nan_to_num(tp.reindex(look).to_numpy(), nan=1.0)

    names, groups = out["item_name"].to_numpy(), out["item_class_group"].to_numpy()
    for hh in range(1, horizon + 1):
        m = (origin + hh).month
        out[f"f_{hh}"] = out["rate"] * season_factor(names, groups, np.full(len(out), m), tables) * out["trend"]
    out["annual"] = out[[f"f_{hh}" for hh in range(1, 13)]].sum(axis=1) if horizon >= 12 else np.nan
    return out


# ── accuracy test ─────────────────────────────────────────────────────────────
def backtest(p, attrs, sales, rules, origins, horizon):
    """Forecast as of each origin month-end (with seasonal patterns from sales up to then) for the next `horizon`
    months, against actual sales and two simple methods. One row per (origin, brick group, branch):
    forecast, actual, last_year (same months a year earlier), avg12 (last 12 months' average × horizon)."""
    rows = []
    by_month = p.set_index(BASE + ["month"])["sold"]
    basis = "avail_company" if rules["fc_availability_basis"] == "company" else "avail"
    avail_month = p.set_index(BASE + ["month"])[basis]
    for origin in origins:
        origin = pd.Period(origin, "M")
        tables = season_tables(sales, rules, until=origin.end_time)
        fc = forecast_at(p, attrs, origin, rules, tables, horizon=horizon)
        if fc.empty:
            continue
        ahead = [origin + h for h in range(1, horizon + 1)]
        idx = fc.index
        def total(months):
            parts = [by_month.reindex(pd.MultiIndex.from_tuples([k + (m,) for k in idx], names=BASE + ["month"]))
                     .fillna(0).to_numpy() for m in months]
            return np.sum(parts, axis=0)
        actual = total(ahead)
        last_year = total([m - 12 for m in ahead])
        avg12 = total([origin - i for i in range(12)]) / 12 * horizon
        look = [avail_month.reindex(pd.MultiIndex.from_tuples([k + (m,) for k in idx], names=BASE + ["month"]))
                .fillna(1.0).to_numpy() for m in ahead]
        in_stock = np.min(look, axis=0)                    # least availability over the months ahead
        rows.append(pd.DataFrame({"origin": str(origin), "brick_group": idx.get_level_values(0),
                                  "branch": idx.get_level_values(1),
                                  "forecast": fc[[f"f_{h}" for h in range(1, horizon + 1)]].sum(axis=1).to_numpy(),
                                  "actual": actual, "last_year": last_year, "avg12": avg12,
                                  "in_stock_ahead": in_stock}))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if len(out):
        out = out.merge(attrs.reset_index(), on=BASE, how="left")
    return out


def accuracy(bt, by):
    """Error per `by` (e.g. ["item_class_group"]) after summing forecasts and actuals to that level per origin:
    WAPE = Σ|forecast − actual| / Σ actual, bias = Σ(forecast − actual) / Σ actual, for the forecast and both
    simple methods; better = the forecast beats both."""
    g = bt.groupby(["origin"] + by)[["forecast", "actual", "last_year", "avg12"]].sum().reset_index()
    out = []
    for key, d in g.groupby(by) if by else [((), g)]:
        act = d["actual"].sum()
        rec = dict(zip(by, key if isinstance(key, tuple) else (key,)))
        rec.update(actual=act, origins=d["origin"].nunique(), forecast=d["forecast"].sum())
        for col, name in (("forecast", "wape"), ("last_year", "wape_last_year"), ("avg12", "wape_avg12")):
            rec[name] = (d[col] - d["actual"]).abs().sum() / act if act > 0 else np.nan
        rec["bias"] = (d["forecast"] - d["actual"]).sum() / act if act > 0 else np.nan
        out.append(rec)
    df = pd.DataFrame(out)
    if len(df):
        df["better"] = (df["wape"] <= df["wape_last_year"]) & (df["wape"] <= df["wape_avg12"])
    return df


def dispersion(bt, by="item_class_group", default=1.5):
    """Spread of actual sales around the forecast, per item group: Σ(actual − forecast)² / Σ forecast at the finest
    level (1 = as random as a Poisson count; higher = lumpier). Sizes the safety margin."""
    d = bt[bt["forecast"] > 0]
    if d.empty:
        return {}, default
    g = d.groupby(by).apply(lambda x: ((x["actual"] - x["forecast"]) ** 2).sum() / x["forecast"].sum(),
                            include_groups=False)
    overall = ((d["actual"] - d["forecast"]) ** 2).sum() / d["forecast"].sum()
    return g.clip(lower=1.0, upper=10.0).to_dict(), float(np.clip(overall, 1.0, 10.0))
