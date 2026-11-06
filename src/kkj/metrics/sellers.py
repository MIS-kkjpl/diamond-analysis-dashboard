"""
Salesperson scorecard: what each salesperson sold, and how it compares with colleagues selling the same things.

Raw totals reward whoever works the busiest counter, so the comparisons are peer-adjusted:
  - discount vs peers: each line's discount against the average discount on lines of the same branch and price
    band (label value), weighted by label value. +2.0 pts = gives 2 points more discount than colleagues did on
    comparable pieces.
  - sales per active month vs the branch median of regular sellers (a month counts when the person billed
    anything; regular = at least MIN_LINES lines sold).
Legacy sale lines carry no salesperson, so the scorecard covers ERP sales only.
Returns are charged to the person who made the original sale, not to whoever booked the return.

  attribute(lines, people)            sale lines -> + code, person, home branch; owners and shared accounts dropped
  scorecard(lines, rules, aged_days)  one row per salesperson
  monthly(lines)                      net sales and bills per person per month
"""
import numpy as np
import pandas as pd

from kkj.metrics.sets import set_id
from kkj.salespeople import split_salesman

MIN_LINES = 20          # fewer sold lines than this: shown, but not flagged


def attribute(lines, people):
    """Sale lines (kkj.db.queries.sales_lines, net of returns via core.net_sales) with the salesperson's code, name
    and kind; a return goes to the seller of the original line when it can be found."""
    out = lines.copy()
    out["code"] = out["salesman"].map(lambda v: split_salesman(v)[0] if isinstance(v, str) and v.strip() else None)
    seller = out[out["pcs"] > 0].dropna(subset=["code"]).drop_duplicates("bill_no").set_index("bill_no")["code"]
    ret = (out["pcs"] < 0) & out["orig_bill_no"].notna()
    out.loc[ret, "code"] = out.loc[ret, "orig_bill_no"].map(seller).fillna(out.loc[ret, "code"])
    p = people.rename(columns={"sales_code": "code", "name": "person", "branch": "home_branch"})
    out = out.merge(p[["code", "person", "home_branch", "kind", "active"]], on="code", how="left")
    out["kind"] = out["kind"].fillna("salesperson")
    out["person"] = out["person"].fillna(out["salesman"].map(lambda v: split_salesman(v)[1] if isinstance(v, str) else v))
    return out[(out["kind"] == "salesperson") & out["code"].notna()]


def _price_band(values, edges):
    bins = [-np.inf] + [float(e) for e in edges] + [np.inf]
    return pd.cut(pd.Series(values, dtype="float"), bins, right=False, labels=False)


def scorecard(lines, rules, aged_days):
    """One row per salesperson over the lines given (already attributed and filtered to a period)."""
    if lines.empty:
        return pd.DataFrame()
    d = lines.copy()
    sold = d["pcs"] > 0
    d["branch_sold"] = d["branch"]
    d["band"] = _price_band(d["item_amt"], rules["price_band_edges"]).values
    # discount = 1 - billed / label value, ERP lines only (legacy lines carry no billed amount)
    has = sold & d["taxable_amount"].notna() & (d["item_amt"] > 0)
    d["disc"] = np.where(has, 1 - d["taxable_amount"] / d["item_amt"].where(d["item_amt"] > 0), np.nan)
    peer = d[has].groupby(["branch_sold", "band"])["disc"].mean().rename("peer_disc")
    d = d.join(peer, on=["branch_sold", "band"])
    d["w"] = np.where(has, d["item_amt"], 0.0)
    d["aged"] = sold & (d["age_days_at_sale"] > aged_days)
    d["unit"] = np.where(sold, d["bill_no"].astype(str) + "|" + d["set_item_barcode"].map(set_id).fillna("")
                         .where(lambda s: s != "", d.index.astype(str)), None)
    d["month"] = d["sale_date"].dt.to_period("M")

    g = d.groupby("code")
    card = pd.DataFrame({
        "person": g["person"].first(), "home_branch": g["home_branch"].first(), "active": g["active"].first(),
        "net_sales": g["net_value"].sum(),
        "gross_sales": d[sold].groupby("code")["net_value"].sum(),
        "returned": -d[~sold].groupby("code")["net_value"].sum(),
        "returns": d[~sold].groupby("code").size(),
        "bills": d[sold].groupby("code")["bill_no"].nunique(),
        "units": d[sold].groupby("code")["unit"].nunique(),
        "lines": d[sold].groupby("code").size(),
        "disc_lines": d[has].groupby("code").size(),
        "discount": d[has].groupby("code").apply(lambda x: np.average(x["disc"], weights=x["w"]) if x["w"].sum() else np.nan,
                                                 include_groups=False),
        "peer_discount": d[has].groupby("code").apply(
            lambda x: np.average(x["peer_disc"], weights=x["w"]) if x["w"].sum() else np.nan, include_groups=False),
        "aged_value": d[d["aged"]].groupby("code")["net_value"].sum(),
        "months": g["month"].nunique(),
        "main_branch": d[sold].groupby("code")["branch_sold"].agg(lambda b: b.value_counts().index[0]),
    })
    for c in ("gross_sales", "returned", "returns", "bills", "units", "lines", "disc_lines", "aged_value"):
        card[c] = card[c].fillna(0)
    card["branch"] = card["main_branch"].fillna(card["home_branch"])
    card["avg_bill"] = card["gross_sales"] / card["bills"].replace(0, np.nan)
    card["units_per_bill"] = card["units"] / card["bills"].replace(0, np.nan)
    card["discount_vs_peers"] = card["discount"] - card["peer_discount"]
    card["return_rate"] = card["returned"] / card["gross_sales"].replace(0, np.nan)
    card["aged_share"] = card["aged_value"] / card["gross_sales"].replace(0, np.nan)
    card["per_month"] = card["net_sales"] / card["months"].replace(0, np.nan)
    # the branch median over regular sellers: occasional helpers would drag it down
    regular = card["per_month"].where(card["lines"] >= MIN_LINES)
    med = regular.groupby(card["branch"]).transform("median")
    card["vs_branch"] = card["per_month"] / med.replace(0, np.nan)
    card["rank"] = card.groupby("branch")["net_sales"].rank(ascending=False, method="min")
    card["of"] = card.groupby("branch")["net_sales"].transform("size")

    company_returns = card["returned"].sum() / card["gross_sales"].sum() if card["gross_sales"].sum() else 0
    branch_aged = card.groupby("branch")["aged_value"].transform("sum") / \
        card.groupby("branch")["gross_sales"].transform("sum").replace(0, np.nan)
    card["notes"] = [_notes(r, company_returns, ba) for r, ba in zip(card.itertuples(), branch_aged)]
    return card.reset_index().sort_values(["branch", "net_sales"], ascending=[True, False])


def _notes(r, company_returns, branch_aged):
    """[(text, colour)] worth a look; only for people with enough lines to judge. Discounts: 2+ points from
    colleagues on similar pieces; returns: at least twice the company rate (and 3%+); old stock: 1.5× the branch share."""
    out = []
    if r.lines < MIN_LINES:
        return [("few sales", "secondary")]
    if r.disc_lines >= MIN_LINES and pd.notna(r.discount_vs_peers):
        if r.discount_vs_peers >= 0.02:
            out.append(("high discounts", "red"))
        elif r.discount_vs_peers <= -0.02:
            out.append(("low discounts", "green"))
    if r.returns >= 3 and pd.notna(r.return_rate) and r.return_rate >= max(2 * company_returns, 0.03):
        out.append(("high returns", "orange"))
    if pd.notna(r.aged_share) and pd.notna(branch_aged) and branch_aged > 0 and r.aged_share >= 1.5 * branch_aged:
        out.append(("clears old stock", "green"))
    return out


def monthly(lines):
    """Net sales and bills per (code, month)."""
    d = lines.assign(month=lines["sale_date"].dt.to_period("M").dt.to_timestamp())
    t = d.groupby(["code", "month"]).agg(net_sales=("net_value", "sum"),
                                         bills=("bill_no", lambda b: b[d.loc[b.index, "pcs"] > 0].nunique()))
    return t.reset_index()


def mix(lines, by="item_class_group"):
    """What a person sold: net sales and pieces by item group (or product)."""
    t = lines.groupby(by, as_index=False).agg(net_sales=("net_value", "sum"), pcs=("net_pcs", "sum"))
    t["share"] = t["net_sales"] / t["net_sales"].sum() if t["net_sales"].sum() else np.nan
    return t.sort_values("net_sales", ascending=False)

