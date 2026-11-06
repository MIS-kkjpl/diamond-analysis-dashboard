"""
Salespeople: a scorecard of everyone who sells, and a page per person (kkj.metrics.sellers).
"""
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from kkj import rules as rules_mod
from kkj.db import queries as q
from kkj.metrics import core as m
from kkj.metrics import sellers as sl
from kkj.metrics.summary import fy_start
from kkj.viz import money
from kkj.web import charts
from kkj.web.common import cached, engine, render

router = APIRouter()


def _short(b):
    return str(b).split(" / ")[0] if b else "–"


def _load(start, end):
    """Attributed ERP sale lines in [start, end], the rules and the dates actually covered."""
    today = pd.Timestamp.today().normalize()
    start_d = pd.Timestamp(start) if start else fy_start(today)
    end_d = pd.Timestamp(end) if end else today
    lines = m.net_sales(cached(q.sales_lines, start_d.date()))
    lines = lines[(lines["sale_date"] >= start_d) & (lines["sale_date"] <= end_d)]
    with engine().connect() as conn:
        people = q.salespeople(conn)
        rules = rules_mod.load(conn)
    erp = lines[lines["source"] != "csv_legacy"]
    return sl.attribute(erp, people), rules, start_d, end_d, (lines["source"] == "csv_legacy").any()


@router.get("/salespeople", response_class=HTMLResponse)
def salespeople(request: Request, start: str = "", end: str = "", branch: list[str] = Query(default=[]),
                show: str = "regular"):
    branch = [b for b in branch if b]
    lines, rules, start_d, end_d, legacy = _load(start, end)
    card = sl.scorecard(lines, rules, rules["aged_days"])
    all_branches = sorted(card["branch"].dropna().unique()) if len(card) else []
    ctx = {"f": {"start": start_d.date().isoformat(), "end": end_d.date().isoformat(), "branch": branch, "show": show},
           "branches": all_branches, "legacy": legacy, "min_lines": sl.MIN_LINES, "aged_days": rules["aged_days"]}
    if card.empty:
        return render(request, "salespeople.html", "salespeople", empty=True, **ctx)
    if branch:
        card = card[card["branch"].isin(branch)]
    shown = card if show == "all" else card[card["lines"] >= sl.MIN_LINES]
    regular = card[card["lines"] >= sl.MIN_LINES]
    gross = card["gross_sales"].sum()
    kpis = [("Salespeople", f"{len(regular):,}", f"with {sl.MIN_LINES}+ lines sold · {len(card):,} in all", "ti-users", "blue"),
            ("Their net sales", money(card["net_sales"].sum()), f"{card['bills'].sum():,.0f} bills", "ti-receipt", "green"),
            ("Average bill", money(gross / card["bills"].sum()) if card["bills"].sum() else "–", "before returns",
             "ti-currency-rupee", "azure"),
            ("Returns", f"{100 * card['returned'].sum() / gross:.1f}%" if gross else "–", "of their sales", "ti-arrow-back-up",
             "orange"),
            ("Worth a look", f"{sum(1 for n in regular['notes'] if any(c in ('red', 'orange') for _, c in n)):,}",
             "high discounts or returns", "ti-flag", "red")]
    return render(request, "salespeople.html", "salespeople", empty=False, kpis=kpis, rows=shown.to_dict("records"),
                  hidden=len(card) - len(shown), short=_short, **ctx)


@router.get("/salespeople/{code}", response_class=HTMLResponse)
def person(request: Request, code: str, start: str = "", end: str = ""):
    lines, rules, start_d, end_d, legacy = _load(start, end)
    card = sl.scorecard(lines, rules, rules["aged_days"])
    if card.empty or code not in set(card["code"]):
        with engine().connect() as conn:
            known = q.salespeople(conn)
        hit = known[known["sales_code"] == code]
        if hit.empty:
            raise HTTPException(404, "No such salesperson")
        return render(request, "salesperson.html", "salespeople", empty=True, p={"code": code, **hit.iloc[0].to_dict()},
                      f={"start": start_d.date().isoformat(), "end": end_d.date().isoformat()}, legacy=legacy)
    r = card[card["code"] == code].iloc[0].to_dict()
    mine = lines[lines["code"] == code]
    branch_lines = lines[lines["branch"] == r["branch"]]
    peers = card[(card["branch"] == r["branch"]) & (card["lines"] >= sl.MIN_LINES)]

    kpis = [("Net sales", money(r["net_sales"]), f"rank {r['rank']:.0f} of {r['of']:.0f} at {_short(r['branch'])}",
             "ti-receipt", "green"),
            ("Bills", f"{r['bills']:,.0f}", f"average {money(r['avg_bill'])} · {r['units_per_bill']:.2f} units each",
             "ti-file-invoice", "blue"),
            ("Discount given", f"{100 * r['discount']:.1f}%" if pd.notna(r["discount"]) else "–",
             f"{100 * r['discount_vs_peers']:+.1f} pts vs colleagues on similar pieces" if pd.notna(r["discount_vs_peers"]) else "",
             "ti-discount", "pink"),
            ("Returns", f"{100 * r['return_rate']:.1f}%" if pd.notna(r["return_rate"]) else "–",
             f"{r['returns']:.0f} lines, {money(r['returned'])}", "ti-arrow-back-up", "orange"),
            (f"Old stock sold (over {rules['aged_days']} days)", f"{100 * r['aged_share']:.0f}%" if pd.notna(r["aged_share"]) else "–",
             "of sales value", "ti-hourglass", "azure")]

    month = sl.monthly(mine)
    fig_month = charts.month_bars(month["month"], month["net_sales"] / 1e5, month["bills"])
    t_month = charts.table(month.drop(columns="code").assign(month=lambda d: d["month"].dt.strftime("%b %Y")),
                           {"net_sales": "money", "bills": "num"})
    mix = sl.mix(mine).merge(sl.mix(branch_lines)[["item_class_group", "share"]].rename(columns={"share": "branch_share"}),
                             on="item_class_group", how="left")
    t_mix = charts.table(mix, {"net_sales": "money", "pcs": "num", "share": "pct", "branch_share": "pct"},
                         {"item_class_group": "item group", "share": "their share", "branch_share": "branch share"})
    t_items = charts.table(sl.mix(mine, "item_name").head(15), {"net_sales": "money", "pcs": "num", "share": "pct"},
                           {"item_name": "product", "share": "their share"})
    peer_t = peers.assign(person=[(p, f"/salespeople/{c}") for p, c in zip(peers["person"], peers["code"])])
    t_peers = charts.table(peer_t[["person", "net_sales", "bills", "avg_bill", "discount", "return_rate", "per_month"]],
                           {"person": "link", "net_sales": "money", "bills": "num", "avg_bill": "money", "discount": "pct",
                            "return_rate": "pct", "per_month": "money"},
                           {"per_month": "per active month", "return_rate": "returns", "avg_bill": "average bill"})
    recent = mine.sort_values("sale_date", ascending=False).head(40)
    t_recent = charts.table(recent[["sale_date", "bill_no", "item_name", "barcode", "pcs", "item_amt", "net_value"]]
                            .assign(barcode=[(b, f"/lookup?q={b}") if isinstance(b, str) and b else ("–", "")
                                             for b in recent["barcode"]]),
                            {"sale_date": "date", "pcs": "num", "item_amt": "money", "net_value": "money", "barcode": "link"},
                            {"sale_date": "date", "item_amt": "label value", "net_value": "billed"})
    return render(request, "salesperson.html", "salespeople", empty=False, p=r, kpis=kpis, short=_short, legacy=legacy,
                  f={"start": start_d.date().isoformat(), "end": end_d.date().isoformat()},
                  fig_month=fig_month, t_month=t_month, t_mix=t_mix, t_items=t_items, t_peers=t_peers, t_recent=t_recent,
                  min_lines=sl.MIN_LINES)
