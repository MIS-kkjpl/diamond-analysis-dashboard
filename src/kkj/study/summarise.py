"""
Level-wise tables for the demand study report, from the files kkj.study.demand_levels wrote to data/study.

    python -m kkj.study.summarise        # writes data/study/report.json and bricks_report.csv
"""
import json

import numpy as np
import pandas as pd

from kkj.study.demand_levels import OUT, PATTERNS

LEVEL_ORDER = ["L1 Up to 2/yr", "L2 2-6/yr", "L3 6-15/yr", "L4 15-40/yr", "L5 Over 40/yr"]


def load():
    p = pd.read_csv(OUT / "bricks.csv", index_col=0)
    tags = pd.read_csv(OUT / "patterns.csv", index_col=0).astype(bool)
    return p, tags


def distribution(p):
    g = p.groupby("level")
    d = pd.DataFrame({
        "bricks": g.size(),
        "pcs_36m": g["pcs_36m"].sum(),
        "value_36m": g["value_36m"].sum(),
    }).reindex(LEVEL_ORDER)
    d["bricks_pct"] = d["bricks"] / d["bricks"].sum()
    d["avg_annual_per_brick"] = d["pcs_36m"] / 3 / d["bricks"]
    d["pcs_pct"] = d["pcs_36m"] / d["pcs_36m"].sum()
    d["value_pct"] = d["value_36m"] / d["value_36m"].sum()
    d["asp"] = d["value_36m"] / d["pcs_36m"]
    return d


def behaviour(p):
    g = p.groupby("level")
    d = pd.DataFrame({
        "avg_month_mean": g["avg_month"].mean(),
        "avg_month_median": g["avg_month"].median(),
        "active_mean": g["active_months"].mean(),
        "active_median": g["active_months"].median(),
        "observed_mean": g["observed_months"].mean(),
        "freq": g["active_months"].sum() / g["observed_months"].sum(),
        "zero_in_stock": g["zero_in_stock"].sum() / g["observed_months"].sum(),
        "zero_no_stock": g["zero_no_stock"].sum() / g["observed_months"].sum(),
        "not_observed": g["not_observed"].sum() / (36 * g.size()),
        "pcs_per_active": g["pcs_36m"].sum() / g["active_months"].sum(),
        "sd_month": g["sd_month"].mean(),
        "cv_median": g["cv"].median(),
        "cv_mean": g["cv"].mean(),
        "zero_12m_pct": g["last_12m"].apply(lambda x: (x <= 0).mean()),
        "zero_24m_pct": g["last_24m"].apply(lambda x: (x <= 0).mean()),
    }).reindex(LEVEL_ORDER)
    d["zero_pct"] = 1 - d["freq"]
    return d


def stability(p):
    g = p.groupby("level")
    d = pd.DataFrame({"y1": g["y1"].sum(), "y2": g["y2"].sum(), "y3": g["y3"].sum()}).reindex(LEVEL_ORDER)
    d["growth_y2"] = d["y2"] / d["y1"] - 1
    d["growth_y3"] = d["y3"] / d["y2"] - 1
    for t in ("increasing", "stable", "decreasing", "too few to tell"):
        d[f"trend_{t}"] = g["trend"].apply(lambda x: (x == t).sum()).reindex(LEVEL_ORDER)
    for c in ("smooth", "intermittent", "erratic", "lumpy", "too few sales"):
        d[f"sbc_{c}"] = g["sbc"].apply(lambda x: (x == c).sum()).reindex(LEVEL_ORDER)
    d["top3_month_share"] = g["top3_month_share"].median()
    d["oct_nov_share"] = g.apply(lambda x: (x["oct_nov_share"] * x["pcs_36m"]).sum() / x["pcs_36m"].sum(),
                                 include_groups=False)
    d["seasonal_bricks"] = g.apply(lambda x: ((x["pcs_36m"] >= 24) & (x["top3_cal_share"] >= 0.4)
                                              & (x["season_corr"] >= 0.3)).sum(), include_groups=False)

    def top_share(x, frac):
        s = x.sort_values(ascending=False)
        k = max(1, int(np.ceil(len(s) * frac)))
        return s.iloc[:k].sum() / s.sum()
    d["top10pct_pcs_share"] = g["pcs_36m"].apply(lambda x: top_share(x, 0.10))
    d["top5_pcs_share"] = g["pcs_36m"].apply(lambda x: x.nlargest(5).sum() / x.sum())
    return d


def financial(p, hv_price):
    g = p.groupby("level")
    low_price = p["asp"].quantile(0.25)
    d = pd.DataFrame({
        "asp_weighted": g["value_36m"].sum() / g["pcs_36m"].sum(),
        "asp_median": g["asp"].median(),
        "asp_p25": g["asp"].quantile(0.25),
        "asp_p75": g["asp"].quantile(0.75),
        "value_36m": g["value_36m"].sum(),
    }).reindex(LEVEL_ORDER)
    d["value_pct"] = d["value_36m"] / d["value_36m"].sum()
    d["top10pct_value_share"] = g["value_36m"].apply(
        lambda x: x.nlargest(max(1, int(np.ceil(len(x) * 0.1)))).sum() / x.sum())
    d["high_value_low_volume"] = g.apply(lambda x: ((x["annual"] <= 6) & (x["asp"] >= hv_price)).sum(),
                                         include_groups=False)
    d["low_value_high_volume"] = g.apply(lambda x: ((x["annual"] > 40) & (x["asp"] <= low_price)).sum(),
                                         include_groups=False)
    return d, float(low_price)


def main():
    p, tags = load()
    meta = json.loads((OUT / "meta.json").read_text())
    dist = distribution(p)
    beh = behaviour(p)
    stab = stability(p)
    fin, low_price = financial(p, meta["hv_price"])
    pat_level = tags.join(p["level"]).groupby("level")[PATTERNS].sum().reindex(LEVEL_ORDER)
    suit_level = pd.crosstab(p["level"], p["suitability"]).reindex(LEVEL_ORDER)
    print(dist.round(3).to_string())
    print(beh.round(3).T.to_string())
    print(stab.round(3).T.to_string())
    print(fin.round(3).T.to_string())
    print(pat_level.T.to_string())
    print(tags.sum().to_string())
    print(suit_level.to_string())
    print("score by level\n", p.groupby("level")["score"].describe().round(1).to_string())
    print("low price", low_price, "hv", meta["hv_price"])


if __name__ == "__main__":
    main()
