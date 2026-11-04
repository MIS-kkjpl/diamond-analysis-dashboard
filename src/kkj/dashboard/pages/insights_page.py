"""Insights: what the stock and sales history say to do, with the numbers behind each point."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from kkj.dashboard import data
from kkj.dashboard.common import GRID, ORDINAL, SERIES, basis_note, chart, cr, indian, lakh, pct, style
from kkj.metrics import core as m
from kkj.metrics import insights as ins


def render():
    st.title("Insights")
    rules = data.rules_now()
    latest = m.company_stock(data.stock_latest())
    if latest.empty:
        st.warning("No production stock snapshot yet.")
        return
    as_of = pd.Timestamp(latest["snapshot_date"].iloc[0])
    history = ins.one_per_week(data.stock_history((as_of - pd.DateOffset(months=rules["insight_months"])).date()))
    start, end = history["snapshot_date"].min(), history["snapshot_date"].max()
    groups = sorted(history["item_class_group"].dropna().unique())
    chosen = st.multiselect("Item group", groups, default=["Diamond Jewellery"] if "Diamond Jewellery" in groups else [])
    if chosen:
        history = history[history["item_class_group"].isin(chosen)]
        latest = latest[latest["item_class_group"].isin(chosen)]

    lines = data.sales_lines(start.date())
    sales = lines[(lines["sale_date"] >= start) & (lines["sale_date"] <= end) & (lines["pcs"] > 0)].copy()
    if chosen:
        sales = sales[sales["item_class_group"].isin(chosen)]
    sales["value"] = m.value(sales)
    sales["age_at_sale"] = sales["age_days_at_sale"]
    sales["discount"] = 1 - sales["taxable_amount"] / sales["item_amt"]
    n_snaps = history["snapshot_date"].nunique()
    st.caption(f"{start:%d %b %Y} to {end:%d %b %Y} · {n_snaps} stock snapshots (one a week at most) · "
               f"{indian(len(sales))} sale lines. Change the period in Settings → Insights.")

    # ── headline: what the slow end costs ──
    latest = latest.assign(value=m.value(latest))
    aged = latest[latest["age_days"] > rules["aged_days"]]
    dead = latest[latest["age_days"] > rules["liquidation_age_days"]]
    recovery = ins.recovery_value(dead, rules["diamond_recovery_pct"] / 100)
    by_age = ins.sell_rate_by_age(history, sales, start, end)
    suggestion = ins.suggested_aged_days(by_age)
    c = st.columns(4)
    c[0].metric(f"Aged stock (over {rules['aged_days']} days)", cr(aged["value"].sum()),
                f"{pct(aged['value'].sum() / latest['value'].sum())} of stock", delta_color="off")
    c[1].metric("Cost of holding it", lakh(ins.carrying_cost_per_month(aged["value"].sum(), rules["capital_cost_pct"])) + " / month",
                f"at {rules['capital_cost_pct']:g}% a year", delta_color="off")
    c[2].metric(f"Liquidation candidates (over {rules['liquidation_age_days']} days)", cr(dead["value"].sum()),
                f"{indian(len(dead))} pieces · recovery ≈ {cr(recovery.sum())}", delta_color="off")
    c[3].metric("Data suggests 'aged' from", f"{suggestion} days" if suggestion else "–",
                f"setting now {rules['aged_days']} days", delta_color="off")

    # ── 1. sell rate by age ──
    st.subheader("1 · Pieces sell slower the longer they sit")
    t = by_age.reset_index(names="age")
    fig = go.Figure(go.Bar(x=t["age"].astype(str), y=100 * t["monthly_sell_rate"],
                           marker_color=(ORDINAL + ORDINAL[-1:] * 3)[: len(t)],
                           customdata=np.c_[t["avg_pcs"], t["avg_value"] / 1e7, t["months_to_sell_half"]],
                           hovertemplate="%{x}<br>%{y:.1f}% sell each month<br>%{customdata[0]:,.0f} pcs held · "
                                         "₹ %{customdata[1]:.1f} Cr<br>half sell in %{customdata[2]:.0f} months<extra></extra>"))
    fig.update_yaxes(ticksuffix="%")
    chart(style(fig, 300, legend=False),
          t.assign(**{"held (pcs)": t["avg_pcs"].round(0), "held (₹ Cr)": (t["avg_value"] / 1e7).round(1),
                      "sold / month": t["sold_per_month"].round(1), "% sold / month": (100 * t["monthly_sell_rate"]).round(1),
                      "months for half to sell": t["months_to_sell_half"].round(1)})
          [["age", "held (pcs)", "held (₹ Cr)", "sold / month", "% sold / month", "months for half to sell"]], "ins_age")
    new_rate = by_age["monthly_sell_rate"].iloc[:2].mean()
    oldest = by_age.iloc[-1]
    st.markdown(f"**What to do:** a new piece sells about **{new_rate / by_age['monthly_sell_rate'].iloc[3]:.1f}×** as fast "
                f"as a 6–12-month-old one. Act on pieces **before** they reach the slow end: at "
                f"{oldest.name} only {100 * oldest['monthly_sell_rate']:.1f}% sell a month, so "
                f"{cr(oldest['avg_value'])} held there needs a liquidation plan, not a discount.")

    # ── 2. suppliers ──
    st.subheader("2 · Suppliers: whose pieces sell, at the same prices")
    bands = [0] + list(rules["price_band_edges"]) + [np.inf]
    sup = ins.supplier_scorecard(history, sales, start, end, bands, rules["min_supplier_pcs"]).dropna(subset=["performance"])
    if len(sup):
        fig = go.Figure(go.Bar(x=sup["performance"], y=sup.index, orientation="h", marker_color=SERIES[0],
                               customdata=np.c_[100 * sup["monthly_sell_rate"], sup["avg_value"] / 1e7, sup["avg_pcs"]],
                               hovertemplate="%{y}<br>%{x:.2f}× the company's rate at its prices<br>"
                                             "%{customdata[0]:.1f}% sold a month · ₹ %{customdata[1]:.2f} Cr held "
                                             "(%{customdata[2]:,.0f} pcs)<extra></extra>"))
        fig.add_vline(x=1, line_color=GRID, line_width=2)
        fig.update_xaxes(ticksuffix="×", showgrid=True, gridcolor=GRID)
        table = sup.assign(**{"performance (×)": sup["performance"].round(2), "% sold / month": (100 * sup["monthly_sell_rate"]).round(1),
                              "held (₹ Cr)": (sup["avg_value"] / 1e7).round(2), "held (pcs)": sup["avg_pcs"].round(0),
                              "turn (×/yr)": sup["annual_turn"].round(2), "median days to sell": sup["median_days_to_sell"],
                              "aged now %": (100 * sup["aged_share_now"]).round(0)}).reset_index(names="supplier")
        chart(style(fig, max(300, 22 * len(sup)), legend=False),
              table[["supplier", "performance (×)", "% sold / month", "held (₹ Cr)", "held (pcs)", "turn (×/yr)",
                     "median days to sell", "aged now %"]], "ins_sup")
        slow = sup[sup["performance"] < 0.5]
        fast = sup[sup["performance"] > 1.5]
        st.markdown(f"**What to do:** buy more from the {len(fast)} suppliers selling over 1.5× the company's rate at their "
                    f"price points ({', '.join(fast.sort_values('performance', ascending=False).index[:5])}); "
                    f"use the {len(slow)} under 0.5× (holding {cr(slow['avg_value'].sum())}) to negotiate exchanges or returns.")
        basis_note("1.0× = the supplier's pieces sell as fast as the company's pieces in the same price bands; prices "
                   "are compared band by band, so a supplier of expensive pieces is not marked slow for being expensive.")

    # ── 3. assortment ──
    st.subheader("3 · Are we holding what customers buy?")
    edges = [-np.inf] + list(rules["price_band_edges"]) + [np.inf]
    names = [f"< {lakh(edges[1], 1)}"] + [f"{lakh(a, 1)} – {lakh(b, 1)}" for a, b in zip(edges[1:-2], edges[2:-1])] + [f"{lakh(edges[-2], 1)} +"]
    h3 = history.assign(band=pd.cut(history["value"], edges, labels=names))
    s3 = sales.assign(band=pd.cut(sales["value"], edges, labels=names))
    gap = ins.assortment_gap(h3, s3, start, end, "band").reset_index(names="band")
    fig = go.Figure()
    fig.add_bar(x=gap["band"].astype(str), y=100 * gap["share_of_stock"], name="Share of stock (pcs)", marker_color=SERIES[0])
    fig.add_bar(x=gap["band"].astype(str), y=100 * gap["share_of_sales"], name="Share of sales (pcs)", marker_color=SERIES[1])
    fig.update_yaxes(ticksuffix="%")
    chart(style(fig, 300), gap.assign(**{"share of stock %": (100 * gap["share_of_stock"]).round(1),
                                         "share of sales %": (100 * gap["share_of_sales"]).round(1),
                                         "months of stock": gap["months_of_stock"].round(1)})
          [["band", "share of stock %", "share of sales %", "months of stock"]], "ins_band")
    items = ins.assortment_gap(history, sales, start, end, "item_name")
    items = items[items["avg_pcs"] >= 20].sort_values("months_of_stock")
    st.dataframe(items.assign(**{"held (pcs)": items["avg_pcs"].round(0), "sold / month": items["sold_per_month"].round(1),
                                 "months of stock": items["months_of_stock"].replace(np.inf, np.nan).round(1),
                                 "share of stock %": (100 * items["share_of_stock"]).round(1),
                                 "share of sales %": (100 * items["share_of_sales"]).round(1)})
                 [["held (pcs)", "sold / month", "months of stock", "share of stock %", "share of sales %"]],
                 width="stretch")
    under = gap[gap["gap"] > 0.02]["band"].astype(str).tolist()
    over = gap[gap["gap"] < -0.02]["band"].astype(str).tolist()
    st.markdown(f"**What to do:** buy towards {', '.join(under) or '–'} (a bigger share of sales than of stock); hold back "
                f"on {', '.join(over) or '–'}. Items at the bottom of the table have the most months of stock: stop "
                f"buying them until it falls; blank months = nothing sold.")

    # ── 4. channels ──
    st.subheader("4 · Exhibitions as a clearance channel")
    kinds = data.branches().set_index("erp_branch")["kind"]
    channel = sales["branch"].map(kinds).fillna("other").map(
        lambda k: "Exhibition" if k == "exhibition" else "Showroom" if k == "showroom" else "Other")
    ch = ins.channel_comparison(sales, channel)
    st.dataframe(ch.assign(value=ch["value"].map(cr), share_over_1y=(100 * ch["share_over_1y"]).round(1),
                           median_discount=(100 * ch.get("median_discount", pd.Series(dtype=float))).round(1))
                 .rename(columns={"share_over_1y": "sold pieces over 1 yr old %", "median_discount": "median discount %",
                                  "median_age_at_sale": "median age at sale (days)"}), width="stretch")
    st.markdown("**What to do:** if exhibitions sell older pieces at a higher discount, send aged pieces there on purpose, "
                "and judge each exhibition on the aged stock it cleared, not only on its sales.")

    # ── 5. liquidation candidates ──
    st.subheader(f"5 · Liquidation candidates: over {rules['liquidation_age_days']} days old")
    liq = dead.assign(recovery=recovery.values, age_years=(dead["age_days"] / 365).round(1))
    liq["recovery_share"] = liq["recovery"] / liq["value"]
    cols = ["barcode", "item_name", "category", "branch", "location", "supplier", "age_years", "net_wt", "dia_wt",
            "value", "recovery", "recovery_share"]
    st.dataframe(liq.sort_values("value", ascending=False)[cols]
                 .assign(value=lambda d: d["value"].map(lakh), recovery=lambda d: d["recovery"].map(lakh),
                         recovery_share=lambda d: (100 * d["recovery_share"]).round(0))
                 .rename(columns={"value": "tag value", "recovery": "recovery value", "recovery_share": "recovery %"}),
                 width="stretch", hide_index=True)
    basis_note(f"Recovery value = metal (net weight × the piece's own per-gram rate) + {rules['diamond_recovery_pct']:g}% of "
               "its diamond value: a floor for deciding between breaking up, returning to the supplier, an exhibition or "
               "an offer. Where recovery is close to the tag value, holding the piece earns nothing.")
