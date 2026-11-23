"""
Builds the demand study report page from data/study (kkj.study.demand_levels + ml_check outputs).

    python -m kkj.study.report        # writes data/study/bricks_report.csv and data/study/report.html
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from kkj.study import summarise as sm
from kkj.study.demand_levels import OUT, PATTERNS, SCORE_PARTS, score_backtest

TEMPLATE = Path(__file__).with_name("report_template.html")
SHORT = {"L1 Up to 2/yr": "L1", "L2 2-6/yr": "L2", "L3 6-15/yr": "L3", "L4 15-40/yr": "L4", "L5 Over 40/yr": "L5"}
RANGE = {"L1": "up to 2 / yr", "L2": "2–6 / yr", "L3": "6–15 / yr", "L4": "15–40 / yr", "L5": "over 40 / yr"}
SHOWN = ["Zero", "Naive (last month)", "Seasonal naive", "SMA 3", "SMA 12", "WMA 6", "EMA (α 0.2)", "SES", "Holt",
         "Holt-Winters", "Weighted annual average", "Croston", "SBA", "Random Forest", "XGBoost", "Prophet",
         "Stockwise (current)"]
BASELINE = {"Zero", "Naive (last month)", "Seasonal naive"}

STRATEGY = {   # level -> (strategy, method, alternative)
    "L1": ("Sales history", "12 / 24 / 36-month sales and weighted annual average", "Plan at product level"),
    "L2": ("Sales history", "12 / 24 / 36-month sales and weighted annual average", "SBA on yearly totals"),
    "L3": ("Simple forecast, 4-month and yearly totals", "EMA (α 0.2)", "SES"),
    "L4": ("Simple forecast, monthly", "SES / EMA (α 0.2)", "XGBoost (pooled), tied"),
    "L5": ("Simple forecast, monthly", "WMA 6", "XGBoost (pooled), within noise"),
}


def rupees_l(x):
    return f"₹{x / 1e5:.2f} L"


def recommend(r, hv_price):
    lv = SHORT[r["level"]]
    usage = f"{r['annual']:.1f} pcs a year, sold in {int(r['active_months'])} of {int(r['observed_months'])} months"
    if r["last_12m"] <= 0:
        return ("No recent sales", "None: review whether to keep the range", "—",
                f"No sale in the last 12 months ({int(r['last_24m'])} in 24). {usage}.")
    if r["suitability"] == "Insufficient data" or lv in ("L1", "L2"):
        s, m, a = STRATEGY["L1"] if lv == "L1" else STRATEGY["L2"]
        why = f"{usage}. At this volume no method beat “no sale” month to month"
        if r["asp"] >= hv_price:
            why += f"; high value ({rupees_l(r['asp'])} a piece), plan piece by piece"
        if lv not in ("L1", "L2"):
            why = f"{usage}. Too little history to test a forecast ({int(r['history_months'])} months)"
            s, m, a = STRATEGY["L1"]
        return s, m, a, why + "."
    s, m, a = STRATEGY[lv]
    notes = []
    if r["trend"] == "increasing":
        notes.append("rising: every method ran 25–35% below actual on rising bricks, check upward")
    if r["trend"] == "decreasing" and lv != "L5":
        notes.append("falling: WMA 6 followed falling bricks best")
        m = "WMA 6"
    if r["sbc"] in ("erratic", "lumpy"):
        notes.append("month sizes vary a lot, read 4-month totals")
    why = f"{usage}, CV {r['cv']:.2f}"
    return s, m, a, why + (". " + "; ".join(notes).capitalize() + "." if notes else ".")


def brick_table(p, tags, hv_price):
    b4 = pd.read_parquet(OUT / "backtest_4m.parquet")
    b4 = b4[b4["method"] == "EMA (α 0.2)"]
    t = b4.groupby(["brick_group", "origin"])[["forecast", "actual"]].sum().reset_index()
    err = t.groupby("brick_group").apply(
        lambda d: (d["forecast"] - d["actual"]).abs().sum() / d["actual"].sum() if d["actual"].sum() > 0 else np.nan,
        include_groups=False)
    rows = []
    for brick, r in p.iterrows():
        s, m, a, why = recommend(r, hv_price)
        rows.append({
            "brick": brick, "item_group": r["item_class_group"], "product": r["item_name"], "level": SHORT[r["level"]],
            "pcs_36m": int(r["pcs_36m"]), "annual": round(r["annual"], 1), "asp": round(r["asp"]),
            "value_36m": round(r["value_36m"]), "active": int(r["active_months"]),
            "observed": int(r["observed_months"]), "zero_pct": round(r["zero_pct"], 3),
            "zero_no_stock": int(r["zero_no_stock"]),
            "cv": None if pd.isna(r["cv"]) else round(r["cv"], 2), "last_12m": int(r["last_12m"]),
            "last_24m": int(r["last_24m"]), "annual_avg": round(r["pcs_36m"] / 3, 1), "trend": r["trend"],
            "sbc": r["sbc"], "patterns": r["patterns"] if isinstance(r["patterns"], str) else "",
            "strategy": s, "method": m, "alternative": a, "score": r["score"], "suitability": r["suitability"],
            "tested_err": None if pd.isna(err.get(brick, np.nan)) else round(float(err[brick]), 3), "reason": why,
        })
    return pd.DataFrame(rows)


def backtests(p):
    lv = p["level"]
    b4 = pd.concat([pd.read_parquet(OUT / "backtest_4m.parquet"), pd.read_parquet(OUT / "backtest_ml.parquet")])
    b12 = pd.read_parquet(OUT / "backtest_12m.parquet")
    out = {}
    for key, df, tot in (("month", b4, False), ("4m", b4, True), ("12m", b12, True)):
        s = score_backtest(df, lv, by_total=tot)
        s["level"] = s["level"].map(SHORT)
        s = s[s["method"].isin(SHOWN)]
        recs = []
        for _, r in s.iterrows():
            recs.append({k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in r.items()})
        out[key] = recs
    # which bricks / origins were tested per method (for the "not tested" note)
    tested = b4.dropna(subset=["forecast"]).merge(lv.rename("level"), left_on="brick_group", right_index=True)
    cov = tested.groupby(["method", "level"])["brick_group"].nunique().unstack(fill_value=0)
    cov.columns = [SHORT[c] for c in cov.columns]
    out["coverage"] = {m: cov.loc[m].to_dict() for m in cov.index}
    out["bricks_per_level"] = {SHORT[k]: int(v) for k, v in lv.value_counts().items()}
    return out


def pattern_scores(p, tags):
    b = pd.concat([pd.read_parquet(OUT / "backtest_4m.parquet"), pd.read_parquet(OUT / "backtest_ml.parquet")])
    b = b.dropna(subset=["forecast"])
    tot = b.groupby(["method", "origin", "brick_group"])[["forecast", "actual"]].sum().reset_index()
    out = {}
    for pat in ["High-volume consistent", "Medium-volume consistent", "Seasonal", "Declining", "Increasing",
                "Irregular / highly variable"]:
        br = tags.index[tags[pat] & (p.loc[tags.index, "annual"] > 6)]
        d = tot[tot["brick_group"].isin(br)]
        if pat == "Seasonal":                       # same bricks for every method (Holt-Winters needs 2 years)
            d = d[d["brick_group"].isin(d[d["method"] == "Holt-Winters"]["brick_group"].unique())]
        r = d.groupby("method").apply(lambda x: pd.Series({
            "wmape": (x["forecast"] - x["actual"]).abs().sum() / x["actual"].sum(),
            "bias": (x["forecast"] - x["actual"]).sum() / x["actual"].sum()}), include_groups=False)
        r = r[r.index.isin(SHOWN) & ~r.index.isin(BASELINE)].sort_values("wmape")
        out[pat] = {"bricks": int(d["brick_group"].nunique()),
                    "rows": [{"method": m, **{k: float(v) for k, v in x.items()}} for m, x in r.iterrows()]}
    # XGBoost vs WMA 6 on L5, origin by origin
    l5 = p.index[p["level"] == "L5 Over 40/yr"]
    d = tot[tot["brick_group"].isin(l5)]
    w = d.groupby(["origin", "method"]).apply(
        lambda x: (x["forecast"] - x["actual"]).abs().sum() / x["actual"].sum(), include_groups=False).unstack()
    diff = w["XGBoost"] - w["WMA 6"]
    out["_xgb_vs_wma"] = {"wins": int((diff < 0).sum()), "origins": int(len(diff)), "mean": float(diff.mean()),
                          "sd": float(diff.std()), "se": float(diff.std() / np.sqrt(len(diff)))}
    return out


def intervals():
    out = {}
    for key in ("iv_month", "iv_4m"):
        d = pd.read_csv(OUT / f"{key}.csv")
        d["level"] = d["level"].map(SHORT)
        out[key] = d.round(4).to_dict(orient="records")
    pi = OUT / "prophet_intervals.parquet"
    if pi.exists():
        d = pd.read_parquet(pi)
        d["origin_p"] = pd.PeriodIndex(d["origin"], freq="M")
        d = d[d["origin_p"] >= pd.Period("2025-07", "M")]
        p = pd.read_csv(OUT / "bricks.csv", index_col=0)["level"].map(SHORT)
        d["level"] = d["brick_group"].map(p)
        res = []
        for (lvl, tot), g in d.groupby(["level", d["h"] == 0]):
            rec = {"level": lvl, "scope": "4m" if tot else "month", "points": len(g)}
            for nom, lo, hi in ((80, "lo80", "hi80"), (95, "lo95", "hi95")):
                low, high = np.floor(g[lo].clip(lower=0)), np.ceil(g[hi].clip(lower=0))
                rec[f"cov_{nom}"] = float(((g["actual"] >= low) & (g["actual"] <= high)).mean())
                rec[f"width_{nom}"] = float((high - low).mean())
            res.append(rec)
        out["prophet"] = res
    return out


def suitability_check(bricks):
    d = bricks.dropna(subset=["tested_err"])
    g = d.groupby("suitability")["tested_err"]
    order = ["Excellent", "Good", "Moderate", "Low", "Insufficient data"]
    q = g.quantile([0.25, 0.5, 0.75]).unstack().reindex(order)
    return [{"band": b, "bricks": int((bricks["suitability"] == b).sum()), "p25": float(q.loc[b, 0.25]),
             "median": float(q.loc[b, 0.5]), "p75": float(q.loc[b, 0.75])} for b in order]


def prose(data):
    """The written parts (answers, method table, interval note, method notes), numbers taken from the results."""
    bt = {(r["level"], r["method"], h): r for h in ("month", "4m", "12m") for r in data["backtest"][h]}

    def w(level, method, h="4m", key="wmape"):
        r = bt.get((level, method, h))
        return r[key] if r else None
    P = lambda x: "—" if x is None else f"{x * 100:.0f}%"
    S = lambda x: "—" if x is None else f"{'+' if x > 0 else '−'}{abs(x) * 100:.0f}%"
    bricks = pd.DataFrame(data["bricks"])
    fc = bricks[bricks["strategy"].str.startswith("Simple forecast")]
    hist = bricks[bricks["strategy"] == "Sales history"]
    inactive = bricks[bricks["strategy"] == "No recent sales"]
    pcs_share = fc["pcs_36m"].sum() / bricks["pcs_36m"].sum()
    val_share = fc["value_36m"].sum() / bricks["value_36m"].sum()
    hv = (hist["asp"] >= data["meta"]["hv_price"]).sum()
    xw = data["pattern_scores"]["_xgb_vs_wma"]
    ex = next(r for r in data["suit_check"] if r["band"] == "Excellent")
    beh, stab = data["beh"], data["stab"]
    iv = {(r["level"], r["method"]): r for r in data["intervals"]["iv_month"]}
    iv4 = {(r["level"], r["method"]): r for r in data["intervals"]["iv_4m"]}
    has_prophet = any(m == "Prophet" for (_, m, _) in bt)
    pr_iv = {(r["level"], r["scope"]): r for r in data["intervals"].get("prophet", [])}
    prophet_txt = (f" Prophet, fitted brick by brick on levels 4–5, was worse than simple smoothing: "
                   f"{P(w('L4', 'Prophet'))} and {P(w('L5', 'Prophet'))} (running {S(w('L5', 'Prophet', '4m', 'bias'))} "
                   f"high on level 5), and its own 80% intervals held only "
                   f"{P(pr_iv[('L5', 'month')]['cov_80'])} of monthly outcomes."
                   if has_prophet and pr_iv else " Prophet was not completed.")
    intermittent = sum(1 for b in data["bricks"] if b["sbc"] in ("intermittent", "lumpy"))
    one_sale = sum(1 for b in data["bricks"] if b["sbc"] == "too few sales")

    answers = [
        ("Which bricks should use machine learning?",
         f"None for now. On level 5, XGBoost had the lowest 4-month error ({P(w('L5', 'XGBoost'))} against "
         f"{P(w('L5', 'WMA 6'))} for a 6-month weighted average), but it won only {xw['wins']} of {xw['origins']} test "
         f"months and the average gain of {xw['mean'] * -100:.1f} points is smaller than its standard error "
         f"({xw['se'] * 100:.1f}). On level 4 it tied with exponential smoothing; on levels 1–3 it was no better or "
         f"worse.{prophet_txt}"),
        ("Which bricks should use mathematical forecasting?",
         f"{len(fc)} bricks: the {int((fc['level'].isin(['L4', 'L5'])).sum())} in levels 4–5 monthly (SES / "
         f"exponential moving average, WMA 6 on level 5), and the {int((fc['level'] == 'L3').sum())} in level 3 on "
         f"4-month and yearly totals only, because month by month no method beat “no sale” there "
         f"({P(w('L3', 'EMA (α 0.2)', 'month'))} vs 100%)."),
        ("Which expensive, slow-moving bricks should use historical averages?",
         f"{len(hist)} bricks get 12, 24 and 36-month sales and a weighted annual average instead of a forecast; "
         f"{hv} of them (plus {int((inactive['asp'] >= data['meta']['hv_price']).sum())} among the bricks with no recent sale) sell at {rupees_l(data['meta']['hv_price'])} a piece or more. Over 12 months, forecasting "
         f"zero for each level 1 brick beat every method tested (best real method {P(w('L1', 'Stockwise (current)', '12m'))} "
         f"error vs 100%)."),
        ("Which bricks have intermittent demand?",
         f"{intermittent} bricks sell in fewer than 3 of 4 months (intermittent or lumpy), plus {one_sale} that sold in "
         f"only one month. Croston and SBA were tested: on level 1 they overshoot by {S(w('L1', 'Croston', '4m', 'bias'))} "
         f"because they never wind down after a brick stops selling; on levels 2–3 they were no better than an "
         f"exponential moving average."),
        ("Which bricks have reliable, predictable demand?",
         f"{ex['bricks']} bricks score Excellent on data, all in levels 4–5. Their tested 4-month error is "
         f"{P(ex['p25'])}–{P(ex['p75'])} (middle half). The {data['patterns']['total']['High-volume consistent']} "
         f"high-volume, consistent bricks are the most predictable group."),
        ("How many bricks can realistically be forecast?",
         f"{len(fc)} of {len(bricks)} ({len(fc) / len(bricks):.0%}), carrying {pcs_share:.0%} of pieces and "
         f"{val_share:.0%} of sales. {len(inactive)} bricks have not sold in 12 months and need a range decision, "
         f"not a forecast."),
        ("How should prediction intervals be calculated?",
         f"As a count distribution around the forecast: negative binomial, variance = φ × forecast, with φ measured "
         f"from each level's recent forecast errors (about {iv[('L4', 'SES')]['phi']:.1f} on level 4, "
         f"{iv[('L5', 'SES')]['phi']:.1f} on level 5). Monthly 80% intervals held {P(iv[('L4', 'SES')]['nb_cov_80'])} "
         f"and {P(iv[('L5', 'SES')]['nb_cov_80'])} of outcomes on levels 4 and 5. On 4-month totals at level 5 they held "
         f"only {P(iv4[('L5', 'SES')]['nb_cov_80'])}, because sales fell below the year they were sized on: φ has to be "
         f"re-measured every month and the hit rate shown. On level 1, {P(iv[('L1', 'SES')]['nb_zero_width_80'])} of "
         f"monthly 80% intervals are just [0, 0], so they say nothing."),
        ("Which approach gives the most reliable results?",
         f"A tiered one: sales history for levels 1–2, smoothing on 4-month and yearly totals for level 3, monthly "
         f"smoothing with count-based intervals for levels 4–5. The current Stockwise forecast stays right for ideal "
         f"stock: over 12 months it is the most accurate on level 5 ({P(w('L5', 'Stockwise (current)', '12m'))} vs "
         f"{P(w('L5', 'SBA', '12m'))} for the next best). Over 4 months, simple smoothing is "
         f"{(w('L4', 'Stockwise (current)') - w('L4', 'EMA (α 0.2)')) * 100:.0f} points better on level 4, where "
         f"Stockwise runs {-w('L4', 'Stockwise (current)', '4m', 'bias') * 100:.0f}% low."),
    ]
    ans_html = "".join(f'<div class="answer"><div class="q">{q}</div><p>{a}</p></div>' for q, a in answers)

    def behaviour(l):
        b, s = beh[l], stab[l]
        return (f"Sold in {b['freq'] * 100:.0f}% of months; {s['sbc_smooth']:.0f} smooth, "
                f"{s['sbc_intermittent']:.0f} intermittent, {s['sbc_erratic'] + s['sbc_lumpy']:.0f} erratic or lumpy"
                + (f", {s['sbc_too few sales']:.0f} with one sale" if s["sbc_too few sales"] else "")
                + f". Median CV {b['cv_median']:.1f}.")
    recs = [
        ("L1", behaviour("L1"), STRATEGY["L1"][1], STRATEGY["L1"][2],
         f"Over 12 months no method beat forecasting zero (best {P(w('L1', 'Stockwise (current)', '12m'))} error vs "
         f"100%). ML ran {S(w('L1', 'XGBoost', '4m', 'bias'))} to {S(w('L1', 'Random Forest', '4m', 'bias'))} high, "
         f"Croston {S(w('L1', 'Croston', '4m', 'bias'))}.",
         f"Whether a piece sells depends on whether it is on display: {beh['L1']['zero_no_stock'] * 100:.0f}% of the "
         f"months it did not sell, it was not in stock anywhere. {beh['L1']['zero_12m_pct'] * 100:.0f}% have not sold "
         f"in 12 months."),
        ("L2", behaviour("L2"), STRATEGY["L2"][1], STRATEGY["L2"][2],
         f"Over 12 months SBA was best ({P(w('L2', 'SBA', '12m'))}) but barely ahead of zero (100%). Over 4 months "
         f"EMA reached {P(w('L2', 'EMA (α 0.2)'))}.",
         "A yearly number is a reference for buying, not a promise; one big sale moves it by a third."),
        ("L3", behaviour("L3"), STRATEGY["L3"][1], STRATEGY["L3"][2],
         f"On 4-month totals EMA {P(w('L3', 'EMA (α 0.2)'))}, SES {P(w('L3', 'SES'))}, SBA {P(w('L3', 'SBA'))}, "
         f"XGBoost {P(w('L3', 'XGBoost'))}. Month by month nothing beat zero.",
         "Monthly numbers are not meaningful; plan in 4-month or yearly blocks."),
        ("L4", behaviour("L4"), STRATEGY["L4"][1], STRATEGY["L4"][2],
         f"4-month error EMA {P(w('L4', 'EMA (α 0.2)'))}, SES {P(w('L4', 'SES'))}, XGBoost {P(w('L4', 'XGBoost'))}, "
         f"Holt-Winters {P(w('L4', 'Holt-Winters'))}, Stockwise {P(w('L4', 'Stockwise (current)'))}.",
         f"This level grew {stab['L4']['growth_y3'] * 100:.0f}% last year and every method ran low "
         f"({S(w('L4', 'EMA (α 0.2)', '4m', 'bias'))} for EMA). Rising bricks need a manual check upward."),
        ("L5", behaviour("L5"), STRATEGY["L5"][1], STRATEGY["L5"][2],
         f"4-month error WMA 6 {P(w('L5', 'WMA 6'))}, SBA {P(w('L5', 'SBA'))}, XGBoost {P(w('L5', 'XGBoost'))}, "
         f"Random Forest {P(w('L5', 'Random Forest'))}"
         + (f", Prophet {P(w('L5', 'Prophet'))}" if has_prophet else "")
         + f", Holt-Winters {P(w('L5', 'Holt-Winters'))}. Seasonal models never won, even on seasonal bricks.",
         f"Level 5 fell {-stab['L5']['growth_y2'] * 100:.0f}% and {-stab['L5']['growth_y3'] * 100:.0f}% in the last two "
         f"years ({stab['L5']['trend_decreasing']:.0f} of 37 bricks falling); a single brick-month is still "
         f"{P(w('L5', 'WMA 6', 'month'))} off on average."),
    ]
    rec_html = ("<thead><tr><th>Level</th><th>Demand behaviour</th><th>Recommended</th><th>Alternative</th>"
                "<th>Reason (tested)</th><th>Expected limitations</th></tr></thead><tbody>"
                + "".join(f'<tr><td><span class="lv"><span class="sw" style="background:var(--l{l[1]})"></span>{l}'
                          f' <span class="sub">{RANGE[l]}</span></span></td><td>{b}</td><td>{m}</td><td>{a}</td>'
                          f"<td>{r}</td><td>{lim}</td></tr>" for l, b, m, a, r, lim in recs) + "</tbody>")

    iv_note = ('<p class="sub">NB = negative binomial count interval; empirical = forecast plus the 10th/90th '
               "(2.5th/97.5th) percentile of past scaled errors, rounded out to whole pieces. Rounding out to whole "
               "pieces is why the empirical intervals hold more than their nominal share on small counts. Amber = more "
               "than 5 points from the nominal share.</p>")

    notes = [
        "Data: ERP sales Jun 2021 – Oct 2026 (Apr–May 2021 left out of training, lockdown months), net of returns; a "
        "set sold counts once; sales of pieces booked against a customer order are left out, as in the Stockwise "
        "forecast. Amount = taxable value (label value where taxable is missing).",
        "Brick = purity · product · weight band, the same grouping as Stock turn and Ideal stock (rule "
        "turn_group_levels). Levels by 36-month net pieces ÷ 3; the 389 bricks are those with a net sale in Nov 2023 – "
        f"Oct 2026 ({data['meta']['all_bricks']} bricks appear in stock or sales since 2021).",
        "Zero months: a month counts as a real “no sale” only after the brick first appeared (in stock or sold). Stock "
        "readings exist for every month; a month with none of the brick in stock anywhere is reported separately as "
        "“not in stock”, since it had no chance to sell.",
        "Demand class (Syntetos–Boylan): ADI = months observed ÷ months with a sale, CV² = squared coefficient of "
        "variation of non-zero months. Smooth ADI < 1.32 and CV² < 0.49; intermittent ADI ≥ 1.32 and CV² < 0.49; "
        "erratic ADI < 1.32 and CV² ≥ 0.49; lumpy both above.",
        f"Patterns: high-volume consistent = over 40 a year and smooth; medium-volume consistent = 6–40 a year and "
        f"smooth; low-volume intermittent = up to 15 a year and ADI ≥ 1.32; high value low volume = up to 6 a year and "
        f"price in the top quarter of bricks (≥ {rupees_l(data['meta']['hv_price'])}); seasonal = 24+ pieces, best 3 "
        "calendar months ≥ 40% of pieces and the monthly profile correlates ≥ 0.3 between years; rising / falling = "
        "last 12 vs previous 12 months differ by ≥ 25% and by 1.96 standard errors (Poisson), needs 8+ pieces; "
        "irregular = 4+ months with a sale and CV² ≥ 0.49.",
        "Backtest: rolling origin. At each starting month every method sees only the sales up to that month; "
        "smoothing parameters are re-fitted each time on that history (least squared one-step error over a small "
        "grid). WMAPE = Σ|forecast − actual| ÷ Σ actual; bias = Σ(forecast − actual) ÷ Σ actual; MAE and RMSE per "
        "brick-month (or per brick-period for totals). MAPE is not used: most brick-months are zero.",
        "Stockwise (current) = the live demand forecast (kkj.metrics.forecast, brick group × branch with borrowing "
        "from product and item group), replayed at each starting month with today's settings and summed over "
        "branches. It is not adjusted for stock held, unlike the accuracy shown on Ideal stock.",
        "Machine learning: Random Forest and XGBoost (Poisson objective) are one model over all bricks, retrained at "
        "every starting month on rows whose outcome was known by then; features are the last 12 months, 3/6/12/24-month "
        "means, months with a sale, months since the last sale, price, age, in stock, calendar month and months ahead. "
        "Prophet is fitted per brick with a yearly pattern, levels 4–5 only and only with 24+ months of history. They "
        "ran in a separate environment; none of these libraries is added to Stockwise.",
        "Not tested: Prophet on levels 1–3, machine learning on the 12-month totals, Holt-Winters for bricks with less "
        "than two years of history. Their cells show “—” or “not tested”.",
        "Caveat: levels are set from the whole 36 months, which overlap the test months, so the backtest describes how "
        "methods do on bricks that turned out to be in each level.",
        "Code: kkj.study (data, methods, demand_levels, ml_check, summarise, report). Read-only; nothing writes to the "
        "database or changes recommendations or ideal stock. Files: data/study/bricks_report.csv has every brick.",
    ]
    notes_html = "".join(f"<li>{n}</li>" for n in notes)
    return ans_html, rec_html, iv_note, notes_html


def main():
    p, tags = sm.load()
    meta = json.loads((OUT / "meta.json").read_text())
    dist = sm.distribution(p)
    beh = sm.behaviour(p)
    stab = sm.stability(p)
    fin, low_price = sm.financial(p, meta["hv_price"])
    pat_level = tags.join(p["level"]).groupby("level")[PATTERNS].sum().reindex(sm.LEVEL_ORDER)
    suit_level = pd.crosstab(p["level"], p["suitability"]).reindex(sm.LEVEL_ORDER).fillna(0).astype(int)
    bricks = brick_table(p, tags, meta["hv_price"])
    bricks.to_csv(OUT / "bricks_report.csv", index=False)

    def frame(df):
        df = df.copy()
        df.index = [SHORT[i] for i in df.index]
        return {i: {k: (None if pd.isna(v) else (float(v) if isinstance(v, (int, float, np.number)) else v))
                    for k, v in r.items()} for i, r in df.iterrows()}

    data = {
        "meta": {**meta, "low_price": low_price, "range": RANGE, "bricks": int(len(p)),
                 "pcs": float(p["pcs_36m"].sum()), "value": float(p["value_36m"].sum()),
                 "all_bricks": int(pd.read_parquet(OUT / "brick_months.parquet", columns=["brick_group"])
                                   ["brick_group"].nunique()),
                 "score_parts": {k: {"weight": w, "how": h} for k, (w, h) in SCORE_PARTS.items()}},
        "dist": frame(dist), "beh": frame(beh), "stab": frame(stab), "fin": frame(fin),
        "patterns": {"total": {k: int(v) for k, v in tags.sum().items()}, "by_level": frame(pat_level)},
        "suit_level": frame(suit_level), "suit_check": suitability_check(bricks),
        "strategy_counts": bricks["strategy"].value_counts().to_dict(),
        "method_counts": bricks["method"].value_counts().to_dict(),
        "backtest": backtests(p), "pattern_scores": pattern_scores(p, tags), "intervals": intervals(),
        "strategy": {k: list(v) for k, v in STRATEGY.items()},
        "bricks": bricks.to_dict(orient="records"),
    }
    blob = json.dumps(data, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    ans, recs, ivn, notes = prose(data)
    html = (TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)
            .replace("<!--__ANSWERS__-->", ans).replace("<!--__RECS__-->", recs)
            .replace("<!--__IVNOTE__-->", ivn).replace("<!--__NOTES__-->", notes))
    (OUT / "report.html").write_text(html, encoding="utf-8")
    print("report", len(bricks), "bricks", len(html) // 1024, "KB")


if __name__ == "__main__":
    main()
