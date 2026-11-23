"""
Forecasting methods for the demand study, numpy only, run on all bricks at once.

Every method takes Y: array (bricks x months) of monthly pieces up to and including the forecast origin, with NaN
before a brick existed, and returns F: (bricks x H) forecasts for the H months after the origin (never below 0).
Smoothing parameters are fitted per brick on the training months only (least one-step-ahead squared error over a
small grid), so a backtest never sees the future.
"""
import numpy as np

ALPHAS = (0.05, 0.1, 0.2, 0.3, 0.5)
BETAS = (0.05, 0.1, 0.2)
GAMMAS = (0.05, 0.1, 0.3)


def _clean(Y):
    return np.nan_to_num(Y, nan=0.0), ~np.isnan(Y)


def _window_mean(Y, k):
    """Mean of the last k observed months (fewer when the brick is younger)."""
    y, ok = _clean(Y[:, -k:])
    n = ok.sum(axis=1)
    return np.where(n > 0, y.sum(axis=1) / np.maximum(n, 1), 0.0)


def _flat(v, H):
    return np.repeat(np.maximum(v, 0)[:, None], H, axis=1)


# ── baselines ────────────────────────────────────────────────────────────────
def zero(Y, H):
    return np.zeros((Y.shape[0], H))


def naive(Y, H):
    return _flat(np.nan_to_num(Y[:, -1]), H)


def seasonal_naive(Y, H):
    """Same month last year."""
    y = np.nan_to_num(Y)
    return np.maximum(np.stack([y[:, -12 + h - 1] for h in range(1, H + 1)], axis=1), 0)


# ── averages ─────────────────────────────────────────────────────────────────
def sma3(Y, H):
    return _flat(_window_mean(Y, 3), H)


def sma12(Y, H):
    return _flat(_window_mean(Y, 12), H)


def wma6(Y, H):
    """Weighted moving average, last 6 months, weights 1..6 (latest heaviest)."""
    y, ok = _clean(Y[:, -6:])
    w = np.arange(1, 7, dtype=float)[None, :] * ok
    return _flat((y * w).sum(axis=1) / np.maximum(w.sum(axis=1), 1e-9), H)


def ema(Y, H, alpha=0.2):
    """Exponential moving average with a fixed alpha (no fitting)."""
    return _flat(_ses_run(Y, np.full(Y.shape[0], alpha))[0], H)


def weighted_annual(Y, H):
    """Weighted annual average: the last three 12-month totals weighted 3:2:1 (latest heaviest), per month."""
    y, ok = _clean(Y)
    tot, wts = 0.0, 0.0
    for i, w in enumerate((3.0, 2.0, 1.0)):
        sl = slice(y.shape[1] - 12 * (i + 1), y.shape[1] - 12 * i)
        months = ok[:, sl].sum(axis=1)
        full = months >= 6                                   # a year counts if at least half of it was observed
        tot = tot + np.where(full, y[:, sl].sum(axis=1) / np.maximum(months, 1) * 12 * w, 0)
        wts = wts + np.where(full, w, 0)
    return _flat(np.where(wts > 0, tot / np.maximum(wts, 1e-9) / 12, 0.0), H)


# ── exponential smoothing ────────────────────────────────────────────────────
def _ses_run(Y, alpha):
    """Level after the last month and in-sample one-step squared error, per brick (alpha per brick)."""
    y, ok = _clean(Y)
    first = np.argmax(ok, axis=1)
    level = y[np.arange(len(y)), first].astype(float)
    sse = np.zeros(len(y))
    for t in range(y.shape[1]):
        use = ok[:, t] & (t > first)
        e = y[:, t] - level
        sse += np.where(use, e * e, 0)
        level = np.where(use, level + alpha * e, level)
    return level, sse


def ses(Y, H):
    best_l, best_s = None, None
    for a in ALPHAS:
        l, s = _ses_run(Y, np.full(Y.shape[0], a))
        if best_s is None:
            best_l, best_s = l, s
        else:
            better = s < best_s
            best_l, best_s = np.where(better, l, best_l), np.where(better, s, best_s)
    return _flat(best_l, H)


def _holt_run(Y, a, b):
    y, ok = _clean(Y)
    first = np.argmax(ok, axis=1)
    level = y[np.arange(len(y)), first].astype(float)
    trend = np.zeros(len(y))
    sse = np.zeros(len(y))
    for t in range(y.shape[1]):
        use = ok[:, t] & (t > first)
        f = level + trend
        e = y[:, t] - f
        sse += np.where(use, e * e, 0)
        new_level = f + a * e
        trend = np.where(use, trend + a * b * e, trend)
        level = np.where(use, new_level, level)
    return level, trend, sse


def holt(Y, H):
    """Holt's linear trend (additive), alpha/beta fitted per brick."""
    best = None
    for a in ALPHAS:
        for b in BETAS:
            l, tr, s = _holt_run(Y, a, b)
            if best is None:
                best = [l, tr, s]
            else:
                m = s < best[2]
                best = [np.where(m, l, best[0]), np.where(m, tr, best[1]), np.where(m, s, best[2])]
    hs = np.arange(1, H + 1)[None, :]
    return np.maximum(best[0][:, None] + hs * best[1][:, None], 0)


def _hw_run(Y, a, b, g, P=12):
    """Additive Holt-Winters. Starts from the first full year the brick was observed; bricks with less than two years
    of history get NaN (they cannot be fitted)."""
    y, ok = _clean(Y)
    n, T = y.shape
    first = np.argmax(ok, axis=1)
    fit = (T - first) >= 2 * P
    level = np.zeros(n)
    season = np.zeros((n, P))
    rows = np.arange(n)
    for i in range(n):
        if fit[i]:
            yr = y[i, first[i]:first[i] + P]
            level[i] = yr.mean()
            season[i] = yr - yr.mean()
    trend = np.zeros(n)
    sse = np.zeros(n)
    for t in range(T):
        use = fit & (t >= first + P)
        pos = t % P
        s = season[:, pos]
        f = level + trend + s
        e = y[:, t] - f
        sse += np.where(use, e * e, 0)
        new_level = level + trend + a * e
        new_trend = trend + a * b * e
        new_s = s + g * (1 - a) * e
        level = np.where(use, new_level, level)
        trend = np.where(use, new_trend, trend)
        season[:, pos] = np.where(use, new_s, s)
    return level, trend, season, np.where(fit, sse, np.nan), T


def holt_winters(Y, H):
    """Additive Holt-Winters (12-month season), parameters fitted per brick. NaN where < 24 months of history."""
    best = None
    for a in (0.1, 0.2, 0.3):
        for b in (0.05, 0.1):
            for g in GAMMAS:
                r = _hw_run(Y, a, b, g)
                if best is None:
                    best = list(r)
                else:
                    m = r[3] < best[3]
                    best = [np.where(m, r[0], best[0]), np.where(m, r[1], best[1]),
                            np.where(m[:, None], r[2], best[2]), np.where(m, r[3], best[3]), r[4]]
    level, trend, season, sse, T = best
    out = np.stack([level + h * trend + season[:, (T + h - 1) % 12] for h in range(1, H + 1)], axis=1)
    out[np.isnan(sse)] = np.nan
    return np.maximum(out, 0)


# ── intermittent demand ──────────────────────────────────────────────────────
def _croston_run(Y, alpha):
    """Croston: smoothed size of non-zero demands (z) and smoothed interval between them (p)."""
    y, ok = _clean(Y)
    n, T = y.shape
    z = np.full(n, np.nan)
    p = np.full(n, np.nan)
    q = np.ones(n)                       # months since the last demand
    sse = np.zeros(n)
    for t in range(T):
        f = np.where(np.isnan(z), 0, z / np.where(np.isnan(p), 1, p))
        e = y[:, t] - f
        sse += np.where(ok[:, t] & ~np.isnan(z), e * e, 0)
        d = ok[:, t] & (y[:, t] > 0)
        start = d & np.isnan(z)
        upd = d & ~np.isnan(z)
        z = np.where(start, y[:, t], np.where(upd, z + alpha * (y[:, t] - z), z))
        p = np.where(start, q, np.where(upd, p + alpha * (q - p), p))
        q = np.where(d, 1, np.where(ok[:, t], q + 1, q))
    return np.where(np.isnan(z), 0, z), np.where(np.isnan(p), 1, p), sse


def _croston_best(Y):
    best = None
    for a in (0.05, 0.1, 0.2, 0.3):
        z, p, s = _croston_run(Y, a)
        if best is None:
            best = [z, p, s, np.full(len(z), a)]
        else:
            m = s < best[2]
            best = [np.where(m, z, best[0]), np.where(m, p, best[1]), np.where(m, s, best[2]),
                    np.where(m, a, best[3])]
    return best


def croston(Y, H):
    z, p, _, _ = _croston_best(Y)
    return _flat(z / p, H)


def sba(Y, H):
    """Syntetos-Boylan approximation: Croston times (1 - alpha/2), which removes Croston's upward bias."""
    z, p, _, a = _croston_best(Y)
    return _flat(z / p * (1 - a / 2), H)


METHODS = {
    "Zero": zero, "Naive (last month)": naive, "Seasonal naive": seasonal_naive,
    "SMA 3": sma3, "SMA 12": sma12, "WMA 6": wma6, "EMA (α 0.2)": ema, "SES": ses,
    "Holt": holt, "Holt-Winters": holt_winters, "Weighted annual average": weighted_annual,
    "Croston": croston, "SBA": sba,
}
BASELINES = ("Zero", "Naive (last month)", "Seasonal naive")
