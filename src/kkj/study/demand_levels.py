"""
Demand study: how each product brick sells, which forecasting approach suits it, tested on history.

    python -m kkj.study.demand_levels            # writes data/study/*.csv and summary.json

Window: the 36 complete months Nov 2023 - Oct 2026. Levels by annual pieces (36-month net pieces / 3), as in the
first cut: <=2, 2-6, 6-15, 15-40, >40. Only bricks with a net sale in the window are profiled (389).

Sections
  profile()     per-brick statistics (volume, value, frequency, variability, trend, seasonality, recency)
  patterns()    behaviour tags (a brick can carry several) and the Syntetos-Boylan demand class
  suitability() transparent 0-100 data-sufficiency score (NOT accuracy)
  backtest()    rolling-origin test of every method in kkj.study.methods + the current Stockwise forecast
  intervals()   80% / 95% prediction intervals: empirical vs negative binomial, coverage on later origins
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from kkj.study import methods as mt

LAST = pd.Period("2026-10", "M")
WINDOW = pd.period_range(LAST - 35, LAST, freq="M")
LEVELS = [(0, 2, "L1 Up to 2/yr"), (2, 6, "L2 2-6/yr"), (6, 15, "L3 6-15/yr"), (15, 40, "L4 15-40/yr"),
          (40, np.inf, "L5 Over 40/yr")]
OUT = Path("data/study")

# Syntetos-Boylan cut-offs (ADI = average months between sales, CV2 = squared CV of non-zero month sizes)
ADI_CUT, CV2_CUT = 1.32, 0.49


def level_of(annual):
    for lo, hi, name in LEVELS:
        if lo < annual <= hi:
            return name
    return None


# ── per-brick profile ────────────────────────────────────────────────────────
def matrix(monthly, months, col="pcs"):
    """bricks x months array of `col`, NaN where the brick did not exist yet."""
    w = monthly[monthly["month"].isin(months)]
    y = w.pivot(index="brick_group", columns="month", values=col).reindex(columns=months)
    obs = w.pivot(index="brick_group", columns="month", values="observed").reindex(columns=months).fillna(False)
    return y.where(obs.astype(bool))


def profile(monthly):
    m = monthly
    win = m[m["month"].isin(WINDOW)]
    tot = win.groupby("brick_group").agg(net_pcs=("net_pcs", "sum"), value=("value", "sum"))
    keep = tot.index[tot["net_pcs"] > 0]
    Y = matrix(m, WINDOW).loc[keep]
    S = win.pivot(index="brick_group", columns="month", values="in_stock").reindex(index=keep, columns=WINDOW)
    y = Y.to_numpy()
    ok = ~np.isnan(y)
    yz = np.nan_to_num(y)
    attrs = m.drop_duplicates("brick_group").set_index("brick_group")[
        ["item_class_group", "purity", "item_name", "category", "weight_band"]].loc[keep]

    p = attrs.copy()
    p["pcs_36m"] = tot.loc[keep, "net_pcs"]
    p["value_36m"] = tot.loc[keep, "value"]
    p["annual"] = p["pcs_36m"] / 3
    p["level"] = p["annual"].map(level_of)
    p["asp"] = p["value_36m"] / p["pcs_36m"]
    p["observed_months"] = ok.sum(axis=1)
    p["history_months"] = m[m["observed"]].groupby("brick_group").size().reindex(keep).to_numpy()
    p["active_months"] = (yz > 0).sum(axis=1)
    p["freq"] = p["active_months"] / p["observed_months"]
    zero = ok & (yz == 0)
    stock = S.fillna(False).to_numpy().astype(bool)
    p["zero_in_stock"] = (zero & stock).sum(axis=1)                 # genuine: on offer, nothing sold
    p["zero_no_stock"] = (zero & ~stock).sum(axis=1)                # nothing anywhere to sell
    p["not_observed"] = (~ok).sum(axis=1)                           # before the brick existed
    p["zero_pct"] = zero.sum(axis=1) / p["observed_months"]
    p["avg_month"] = np.nanmean(y, axis=1)
    p["median_month"] = np.nanmedian(y, axis=1)
    p["sd_month"] = np.nanstd(y, axis=1, ddof=1)
    p["cv"] = p["sd_month"] / p["avg_month"].where(p["avg_month"] > 0)
    p["pcs_per_active"] = np.where(p["active_months"] > 0, yz.sum(axis=1) / np.maximum(p["active_months"], 1), 0)

    # demand class (Syntetos-Boylan) on the observed months
    nz = np.where(yz > 0, yz, np.nan)
    mean_nz = np.nanmean(nz, axis=1)
    sd_nz = np.nanstd(nz, axis=1)
    p["adi"] = p["observed_months"] / p["active_months"].where(p["active_months"] > 0)
    p["cv2"] = np.where(p["active_months"] >= 2, (sd_nz / mean_nz) ** 2, np.nan)
    p["sbc"] = np.select(
        [p["active_months"] < 2, (p["adi"] < ADI_CUT) & (p["cv2"] < CV2_CUT), (p["adi"] >= ADI_CUT) & (p["cv2"] < CV2_CUT),
         (p["adi"] < ADI_CUT)], ["too few sales", "smooth", "intermittent", "erratic"], "lumpy")

    # recency and years
    for k, name in ((12, "last_12m"), (24, "last_24m")):
        p[name] = yz[:, -k:].sum(axis=1)
    p["active_12m"] = (yz[:, -12:] > 0).sum(axis=1)
    p["y1"], p["y2"], p["y3"] = yz[:, :12].sum(axis=1), yz[:, 12:24].sum(axis=1), yz[:, 24:].sum(axis=1)

    # trend: last 12 vs the 12 before, Poisson z on the difference
    a, b = p["y3"], p["y2"]
    z = (a - b) / np.sqrt((a + b).where(a + b > 0))
    ratio = (a + 0.5) / (b + 0.5)
    p["trend"] = np.select([(a + b) < 8, (z >= 1.96) & (ratio >= 1.25), (z <= -1.96) & (ratio <= 0.8)],
                           ["too few to tell", "increasing", "decreasing"], "stable")
    p["trend_ratio"] = ratio

    # concentration in time: share of 36-month pieces in the brick's best 3 months
    p["top3_month_share"] = np.sort(yz, axis=1)[:, -3:].sum(axis=1) / np.maximum(yz.sum(axis=1), 1e-9)

    # seasonality: calendar-month profile, consistent across the three years
    cal = np.zeros((len(p), 12))
    years = np.stack([yz[:, i * 12:(i + 1) * 12] for i in range(3)], axis=0)       # (3, n, 12) Nov..Oct
    cal = years.sum(axis=0)
    p["top3_cal_share"] = np.sort(cal, axis=1)[:, -3:].sum(axis=1) / np.maximum(cal.sum(axis=1), 1e-9)
    cors = []
    for i, j in ((0, 1), (0, 2), (1, 2)):
        xi, xj = years[i], years[j]
        xi_c, xj_c = xi - xi.mean(axis=1, keepdims=True), xj - xj.mean(axis=1, keepdims=True)
        den = np.sqrt((xi_c ** 2).sum(axis=1) * (xj_c ** 2).sum(axis=1))
        cors.append(np.where(den > 0, (xi_c * xj_c).sum(axis=1) / np.where(den > 0, den, 1), np.nan))
    p["season_corr"] = np.nanmean(np.stack(cors), axis=0)
    # Oct-Nov (Dhanteras / Diwali) share: Nov is column 0, Oct is column 11 of each year
    p["oct_nov_share"] = (cal[:, 0] + cal[:, 11]) / np.maximum(cal.sum(axis=1), 1e-9)
    return p


# ── behaviour tags ───────────────────────────────────────────────────────────
PATTERNS = ["High-volume consistent", "Medium-volume consistent", "Low-volume intermittent", "High-value low-volume",
            "Seasonal", "Declining", "Increasing", "Irregular / highly variable", "No sales in last 12 months"]


def patterns(p):
    hv_price = p["asp"].quantile(0.75)
    consistent = (p["adi"] < ADI_CUT) & (p["cv2"] < CV2_CUT)
    t = pd.DataFrame(index=p.index)
    t["High-volume consistent"] = (p["annual"] > 40) & consistent
    t["Medium-volume consistent"] = (p["annual"] > 6) & (p["annual"] <= 40) & consistent
    t["Low-volume intermittent"] = (p["annual"] <= 15) & (p["adi"] >= ADI_CUT)
    t["High-value low-volume"] = (p["annual"] <= 6) & (p["asp"] >= hv_price)
    t["Seasonal"] = (p["pcs_36m"] >= 24) & (p["top3_cal_share"] >= 0.40) & (p["season_corr"] >= 0.30)
    t["Declining"] = p["trend"] == "decreasing"
    t["Increasing"] = p["trend"] == "increasing"
    t["Irregular / highly variable"] = (p["active_months"] >= 4) & (p["cv2"] >= CV2_CUT)
    t["No sales in last 12 months"] = p["last_12m"] <= 0
    return t, float(hv_price)


# ── data-sufficiency score ───────────────────────────────────────────────────
SCORE_PARTS = {   # name: (weight, how it is measured)
    "history": (10, "months of history / 48, capped at 1"),
    "active": (20, "months with a sale in 36 / 24, capped at 1"),
    "frequency": (15, "months with a sale / months observed (36-month window)"),
    "stability": (20, "1 - CV of monthly pieces / 2, floored at 0"),
    "recent": (15, "months with a sale in the last 12 / 6, capped at 1"),
    "volume": (20, "log(1 + pieces a year) / log(1 + 60), capped at 1"),
}


def suitability(p):
    parts = pd.DataFrame(index=p.index)
    parts["history"] = (p["history_months"] / 48).clip(upper=1)
    parts["active"] = (p["active_months"] / 24).clip(upper=1)
    parts["frequency"] = p["freq"].clip(0, 1)
    parts["stability"] = (1 - p["cv"].fillna(9) / 2).clip(0, 1)
    parts["recent"] = (p["active_12m"] / 6).clip(upper=1)
    parts["volume"] = (np.log1p(p["annual"].clip(lower=0)) / np.log1p(60)).clip(upper=1)
    score = sum(parts[k] * w for k, (w, _) in SCORE_PARTS.items())
    insufficient = (p["history_months"] < 24) | (p["active_months"] < 4) | (p["pcs_36m"] < 6)
    band = np.select([insufficient, score >= 80, score >= 65, score >= 45],
                     ["Insufficient data", "Excellent", "Good", "Moderate"], "Low")
    return score.round(1), pd.Series(band, index=p.index), parts


# ── backtest ─────────────────────────────────────────────────────────────────
def _metrics(f, a):
    e = f - a
    s = a.sum()
    return {"n": len(a), "actual": float(s), "forecast": float(f.sum()), "mae": float(np.abs(e).mean()),
            "rmse": float(np.sqrt((e ** 2).mean())), "wmape": float(np.abs(e).sum() / s) if s > 0 else np.nan,
            "bias": float(e.sum() / s) if s > 0 else np.nan}


def backtest(monthly, bricks, origins, H, engine=None):
    """Long frame: origin, brick_group, h, method, forecast, actual. Training data = every month up to the origin.
    engine: optional {origin: DataFrame index brick_group, columns f_1..f_H} (the current Stockwise forecast)."""
    all_months = pd.period_range(monthly["month"].min(), LAST, freq="M")
    Yall = matrix(monthly, all_months).reindex(bricks)
    A = matrix(monthly, all_months).reindex(bricks).fillna(0)
    rows = []
    for o in origins:
        i = all_months.get_loc(o)
        Y = Yall.iloc[:, :i + 1].to_numpy()
        act = A.iloc[:, i + 1:i + 1 + H].to_numpy()
        fc = {name: fn(Y, H) for name, fn in mt.METHODS.items()}
        if engine is not None and o in engine:
            e = engine[o].reindex(bricks)
            fc["Stockwise (current)"] = e[[f"f_{h}" for h in range(1, H + 1)]].fillna(0).to_numpy()
        for name, F in fc.items():
            for h in range(H):
                rows.append(pd.DataFrame({"origin": str(o), "brick_group": bricks, "h": h + 1, "method": name,
                                          "forecast": F[:, h], "actual": act[:, h]}))
    return pd.concat(rows, ignore_index=True)


def score_backtest(bt_, levels, by_total=False):
    """Metrics per level and method. by_total: on the sum over the horizon per brick and origin (planning bucket)."""
    d = bt_.dropna(subset=["forecast"]).merge(levels.rename("level"), left_on="brick_group", right_index=True)
    if by_total:
        d = d.groupby(["level", "method", "origin", "brick_group"])[["forecast", "actual"]].sum().reset_index()
    out = []
    for (lvl, meth), g in d.groupby(["level", "method"]):
        out.append({"level": lvl, "method": meth, **_metrics(g["forecast"].to_numpy(), g["actual"].to_numpy())})
    return pd.DataFrame(out)


# ── prediction intervals ─────────────────────────────────────────────────────
def nb_quantile(mean, phi, q):
    """Quantile of a count with this mean and variance phi x mean (negative binomial; Poisson when phi <= 1)."""
    mean = np.asarray(mean, dtype=float)
    out = np.zeros(len(mean))
    kmax = int(max(10, np.ceil(mean.max() * 6 + 30))) if len(mean) else 10
    k = np.arange(kmax + 1)
    from math import lgamma
    lg = np.vectorize(lgamma)
    for i, mu in enumerate(mean):
        if mu <= 1e-9:
            continue
        if phi <= 1.0001:
            logp = k * np.log(mu) - mu - lg(k + 1)
        else:
            r = mu / (phi - 1)
            pr = 1 / phi
            logp = lg(k + r) - lg(r) - lg(k + 1) + r * np.log(pr) + k * np.log(1 - pr)
        cdf = np.cumsum(np.exp(logp))
        out[i] = k[np.searchsorted(cdf, q)] if cdf[-1] >= q else kmax
    return out


def intervals(bt_, levels, methods, calib, test, total=False):
    """For each level x method: coverage and width of 80% / 95% intervals on the TEST origins, sized only from
    errors on the CALIBRATION origins (whose actuals were all known before the first test origin).
      empirical: forecast + quantiles of the scaled error (actual - forecast) / sqrt(forecast + 0.5)
      neg. binomial: count distribution with the forecast as mean and variance phi x mean (phi from calibration)"""
    d = bt_[bt_["method"].isin(methods)].dropna(subset=["forecast"]).merge(
        levels.rename("level"), left_on="brick_group", right_index=True)
    if total:
        d = d.groupby(["level", "method", "origin", "brick_group"])[["forecast", "actual"]].sum().reset_index()
    d["origin_p"] = pd.PeriodIndex(d["origin"], freq="M")
    out = []
    for (lvl, meth), g in d.groupby(["level", "method"]):
        c = g[g["origin_p"].isin(calib)]
        t = g[g["origin_p"].isin(test)]
        if c.empty or t.empty:
            continue
        r = (c["actual"] - c["forecast"]) / np.sqrt(c["forecast"] + 0.5)
        phi = float(((c["actual"] - c["forecast"]) ** 2).sum() / max(c["forecast"].sum(), 1e-9))
        f, a = t["forecast"].to_numpy(), t["actual"].to_numpy()
        rec = {"level": lvl, "method": meth, "phi": phi, "test_points": len(t), "mean_forecast": float(f.mean()),
               "mean_actual": float(a.mean())}
        for nom in (0.80, 0.95):
            lo_q, hi_q = (1 - nom) / 2, 1 - (1 - nom) / 2
            s = np.sqrt(f + 0.5)
            lo = np.floor(np.maximum(f + np.quantile(r, lo_q) * s, 0))
            hi = np.ceil(np.maximum(f + np.quantile(r, hi_q) * s, 0))
            rec[f"emp_cov_{int(nom*100)}"] = float(((a >= lo) & (a <= hi)).mean())
            rec[f"emp_width_{int(nom*100)}"] = float((hi - lo).mean())
            lo_nb = nb_quantile(f, phi, lo_q)
            hi_nb = nb_quantile(f, phi, hi_q)
            rec[f"nb_cov_{int(nom*100)}"] = float(((a >= lo_nb) & (a <= hi_nb)).mean())
            rec[f"nb_width_{int(nom*100)}"] = float((hi_nb - lo_nb).mean())
            rec[f"nb_zero_width_{int(nom*100)}"] = float((hi_nb == 0).mean())   # interval is just [0, 0]
        out.append(rec)
    return pd.DataFrame(out)


# ── current Stockwise forecast, replayed at each origin ──────────────────────
def engine_forecasts(origins, H):
    """{origin: company forecast per brick group, f_1..f_H} from kkj.metrics.forecast (summed over branches)."""
    from datetime import date
    from kkj import rules as rules_mod, settings
    from kkj.brain.run import forecast_dates
    from kkj.db import queries as q
    from kkj.metrics import brick_turn as bt, forecast as fc, sets
    with settings.app_engine().connect() as conn:
        rules = rules_mod.load(conn)
        dates = q.stock_dates(conn)["snapshot_date"]
        stock = bt.add_brick(sets.stock_units(q.brick_stock(conn, forecast_dates(dates, LAST.end_time))), rules)
        sales = bt.add_brick(sets.sales_units(q.brick_sales(conn, date(2021, 4, 1))), rules)
    p, attrs = fc.panel(stock, sales, rules, LAST)
    out = {}
    for o in origins:
        f = fc.forecast_at(p, attrs, o, rules, fc.season_tables(sales, rules, until=o.end_time), horizon=H)
        if len(f):
            out[o] = f[[f"f_{h}" for h in range(1, H + 1)]].groupby(level="brick_group").sum()
    return out


def main():
    from kkj.study import data
    OUT.mkdir(parents=True, exist_ok=True)
    src = OUT / "brick_months.parquet"
    if src.exists():
        monthly = pd.read_parquet(src)
        monthly["month"] = pd.PeriodIndex(monthly["month"], freq="M")
    else:
        _, monthly = data.load(str(LAST))

    p = profile(monthly)
    tags, hv_price = patterns(p)
    p["score"], p["suitability"], parts = suitability(p)
    p["patterns"] = tags.apply(lambda r: "; ".join(c for c in PATTERNS if r[c]), axis=1)

    # monthly backtest: 24 origins, 4 months ahead
    H = 4
    origins = list(pd.period_range("2024-07", "2026-06", freq="M"))
    origins12 = list(pd.period_range("2024-10", "2025-10", freq="M"))
    eng = engine_forecasts(sorted(set(origins) | set(origins12)), 12)
    bt4 = backtest(monthly, p.index, origins, H, eng)
    # annual backtest: 13 origins, 12 months ahead
    bt12 = backtest(monthly, p.index, origins12, 12, eng)

    lv = p["level"]
    s_month = score_backtest(bt4, lv)
    s_4m = score_backtest(bt4, lv, by_total=True)
    s_12m = score_backtest(bt12, lv, by_total=True)

    calib = list(pd.period_range("2024-07", "2025-03", freq="M"))   # actuals known by Jul 2025
    test = list(pd.period_range("2025-07", "2026-06", freq="M"))
    meths = ["SES", "SBA", "Croston", "Weighted annual average", "SMA 12", "Holt-Winters", "Stockwise (current)"]
    iv_month = intervals(bt4, lv, meths, calib, test)
    iv_4m = intervals(bt4, lv, meths, calib, test, total=True)

    p.to_csv(OUT / "bricks.csv")
    tags.to_csv(OUT / "patterns.csv")
    parts.to_csv(OUT / "score_parts.csv")
    bt4.to_parquet(OUT / "backtest_4m.parquet", index=False)
    bt12.to_parquet(OUT / "backtest_12m.parquet", index=False)
    for name, df in (("score_month", s_month), ("score_4m", s_4m), ("score_12m", s_12m), ("iv_month", iv_month),
                     ("iv_4m", iv_4m)):
        df.to_csv(OUT / f"{name}.csv", index=False)
    (OUT / "meta.json").write_text(json.dumps({"hv_price": hv_price, "window": [str(WINDOW[0]), str(WINDOW[-1])],
                                               "origins_4m": [str(origins[0]), str(origins[-1])],
                                               "origins_12m": [str(origins12[0]), str(origins12[-1])]}))
    print("done", len(p))


if __name__ == "__main__":
    main()
