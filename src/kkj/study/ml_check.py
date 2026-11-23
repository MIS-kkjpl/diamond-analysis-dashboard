"""
Machine-learning check for the demand study: Random Forest, XGBoost (pooled over all bricks) and Prophet (per
brick, levels 4-5), tested on the same rolling origins as kkj.study.demand_levels.

Needs scikit-learn, xgboost and prophet, which are NOT project dependencies: run it in a separate environment.
It reads only data/study/brick_months.parquet (written by kkj.study.data) and does not import kkj.

    <venv>/python src/kkj/study/ml_check.py [data/study]

Writes backtest_ml.parquet (origin, brick_group, h, method, forecast, actual) and prophet_intervals.parquet.
"""
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
D = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), "data/study"))
LAST = pd.Period("2026-10", "M")
WINDOW = pd.period_range(LAST - 35, LAST, freq="M")
ORIGINS = list(pd.period_range("2024-07", "2026-06", freq="M"))
H = 4


def load():
    m = pd.read_parquet(D / "brick_months.parquet")
    m["month"] = pd.PeriodIndex(m["month"], freq="M")
    win = m[m["month"].isin(WINDOW)].groupby("brick_group")["net_pcs"].sum()
    bricks = win.index[win > 0]
    m = m[m["brick_group"].isin(bricks)]
    months = pd.period_range(m["month"].min(), LAST, freq="M")
    piv = lambda col: m.pivot(index="brick_group", columns="month", values=col).reindex(index=bricks, columns=months)
    obs = piv("observed").fillna(False).astype(bool).to_numpy()
    y = piv("pcs").fillna(0).to_numpy()
    val = piv("value").fillna(0).clip(lower=0).to_numpy()
    stock = piv("in_stock").fillna(False).astype(float).to_numpy()
    lvl = (win.loc[bricks] / 3)
    return bricks, months, y, obs, val, stock, lvl


def features(y, obs, val, stock, months, t, h):
    """Feature rows for every brick at month index t, forecasting t + h. Only data up to t is used."""
    n = y.shape[0]
    f = {}
    for k in range(12):
        f[f"lag{k+1}"] = y[:, t - k] if t - k >= 0 else np.zeros(n)
    hist = y[:, max(0, t - 11):t + 1]
    f["mean3"] = y[:, max(0, t - 2):t + 1].mean(axis=1)
    f["mean6"] = y[:, max(0, t - 5):t + 1].mean(axis=1)
    f["mean12"] = hist.mean(axis=1)
    f["mean24"] = y[:, max(0, t - 23):t + 1].mean(axis=1)
    f["active12"] = (hist > 0).sum(axis=1)
    f["months_since_sale"] = np.where((y[:, :t + 1] > 0).any(axis=1), np.argmax((y[:, :t + 1] > 0)[:, ::-1], axis=1), 99)
    cp, cv = y[:, :t + 1].sum(axis=1), val[:, :t + 1].sum(axis=1)
    f["log_price"] = np.log1p(np.where(cp > 0, cv / np.maximum(cp, 1), 0))
    f["age"] = obs[:, :t + 1].sum(axis=1)
    f["in_stock"] = stock[:, t]
    moy = (months[t] + h).month
    f["h"] = np.full(n, h)
    f["moy_sin"] = np.full(n, np.sin(2 * np.pi * moy / 12))
    f["moy_cos"] = np.full(n, np.cos(2 * np.pi * moy / 12))
    f["is_oct_nov"] = np.full(n, float(moy in (10, 11)))
    return pd.DataFrame(f)


def pooled(bricks, months, y, obs, val, stock):
    from sklearn.ensemble import RandomForestRegressor
    from xgboost import XGBRegressor
    rows = []
    start = 12
    cache = {}
    for t in range(start, len(months)):
        for h in range(1, H + 1):
            X = features(y, obs, val, stock, months, t, h)
            ok = obs[:, t]
            target = y[:, t + h] if t + h < len(months) else np.full(len(bricks), np.nan)
            cache[(t, h)] = (X[ok], target[ok], np.where(ok)[0])
    for o in ORIGINS:
        oi = months.get_loc(o)
        Xs, ys = [], []
        for (t, h), (X, tg, _) in cache.items():
            if t + h <= oi:                                   # the target month was known by the origin
                Xs.append(X)
                ys.append(tg)
        Xtr, ytr = pd.concat(Xs, ignore_index=True), np.concatenate(ys)
        rf = RandomForestRegressor(n_estimators=300, min_samples_leaf=5, max_features=0.5, n_jobs=-1, random_state=0)
        rf.fit(Xtr, ytr)
        xg = XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                          objective="count:poisson", n_jobs=-1, random_state=0)
        xg.fit(Xtr, ytr)
        for h in range(1, H + 1):
            X = features(y, obs, val, stock, months, oi, h)
            act = y[:, oi + h]
            for name, model in (("Random Forest", rf), ("XGBoost", xg)):
                rows.append(pd.DataFrame({"origin": str(o), "brick_group": bricks, "h": h, "method": name,
                                          "forecast": np.maximum(model.predict(X), 0), "actual": act}))
        print("pooled", o, len(Xtr), flush=True)
    return pd.concat(rows, ignore_index=True)


def prophet_run(bricks, months, y, obs, lvl):
    import logging
    from prophet import Prophet
    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
    logging.getLogger("prophet").setLevel(logging.ERROR)
    sel = np.where(lvl.to_numpy() > 15)[0]
    rows, ivs = [], []
    ds = months.to_timestamp()
    for o in ORIGINS:
        oi = months.get_loc(o)
        t0 = time.time()
        for i in sel:
            ok = obs[i, :oi + 1]
            if ok.sum() < 24:                    # a yearly pattern needs two years: not tested for this brick
                continue
            df = pd.DataFrame({"ds": ds[:oi + 1][ok], "y": y[i, :oi + 1][ok]})
            m = Prophet(yearly_seasonality=True, weekly_seasonality=False, daily_seasonality=False,
                        uncertainty_samples=500)
            m.fit(df)
            fut = pd.DataFrame({"ds": ds[oi + 1:oi + 1 + H]})
            point = np.maximum(m.predict(fut)["yhat"].to_numpy(), 0)
            samp = np.maximum(m.predictive_samples(fut)["yhat"], 0)
            act = y[i, oi + 1:oi + 1 + H]
            rows.append(pd.DataFrame({"origin": str(o), "brick_group": bricks[i], "h": range(1, H + 1),
                                      "method": "Prophet", "forecast": point, "actual": act}))
            q = np.quantile(samp, [0.025, 0.10, 0.90, 0.975], axis=1)
            tot = np.quantile(samp.sum(axis=0), [0.025, 0.10, 0.90, 0.975])
            ivs.append(pd.DataFrame({"origin": str(o), "brick_group": bricks[i], "h": list(range(1, H + 1)) + [0],
                                     "lo95": list(q[0]) + [tot[0]], "lo80": list(q[1]) + [tot[1]],
                                     "hi80": list(q[2]) + [tot[2]], "hi95": list(q[3]) + [tot[3]],
                                     "actual": list(act) + [act.sum()]}))
        print("prophet", o, f"{time.time() - t0:.0f}s", flush=True)
    return pd.concat(rows, ignore_index=True), pd.concat(ivs, ignore_index=True)


if __name__ == "__main__":
    bricks, months, y, obs, val, stock, lvl = load()
    if "--prophet-only" in sys.argv:
        ml = pd.read_parquet(D / "backtest_ml.parquet")
        ml = ml[ml["method"] != "Prophet"]
    else:
        ml = pooled(bricks, months, y, obs, val, stock)
        ml.to_parquet(D / "backtest_ml.parquet", index=False)
    pr, iv = prophet_run(bricks, months, y, obs, lvl)
    pd.concat([ml, pr], ignore_index=True).to_parquet(D / "backtest_ml.parquet", index=False)
    iv.to_parquet(D / "prophet_intervals.parquet", index=False)
    print("done")
