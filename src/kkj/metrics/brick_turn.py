"""
Weighted stock turn per brick, against an ideal turn (the Stock_ETL requirement, rebuilt).

A brick is a group of interchangeable pieces: Purity – Product – Variety – Type – Weight range
(e.g. 18KT · D RING · MICRO · 0.25–0.5ct). For diamond jewellery the weight range is the total
diamond weight in carats, the usual planning measure (gross metal weight is the gold-jewellery
one); both the levels and the weight basis are business rules.

For each brick, over three windows ending on the latest stock date:

  window   fortnights   needs stock data in   counts as available when
  3 months      6       >= turn_history_pct   in stock (pcs > 0) in >= turn_availability_pct
  6 months     13       of its fortnights     of the fortnights that have data
  12 months    26

  average stock = mean pcs over the fortnights with data, ZEROS INCLUDED
  turn          = net pcs sold in the window, annualised / average stock

Blend (weights are rules):  3+6+12 qualify -> 60/30/10;  6+12 -> 75/25;  3+6 (new) -> 75/25.
Only 3 months qualifies -> "too new". Anything else is not qualified: its 12-month turn is used
with the exception check below.

Status, with ideal = the ideal-turn rule and band = turn_band_pct:
  blended turn within ideal ± band          -> keep
  below                                     -> reduce   (more stock than its sales need)
  above                                     -> increase (sells faster than it is stocked)
  not qualified and out of range: if the neighbouring weight ranges of the same brick are both
  within range -> hold (keep for turn_hold_months more); else phase out (below) or
  stock up (above: sells whenever it is in stock).
  ideal stock (pcs) = blended annual sales / ideal turn.

Fixes over the Stock_ETL version: bricks with stock but no sales are kept (turn 0, dead stock),
zero-stock fortnights count in the average, missing or quarantined snapshots don't count
against availability, and a new launch is shown as "too new" instead of disappearing.
"""
import numpy as np
import pandas as pd

WINDOWS = {"3m": 6, "6m": 13, "12m": 26}            # fortnights per window
FORTNIGHT = 14
LEVELS = ("purity", "item_name", "category", "sub_category", "weight_band")
LEVEL_LABELS = {"purity": "Purity", "item_name": "Product", "category": "Variety", "sub_category": "Type",
                "weight_band": "Weight range"}
STATUS = {  # status -> (label, colour, what to do)
    "increase": ("Increase", "green", "sells faster than it is stocked"),
    "reduce": ("Reduce", "orange", "more stock than its sales need"),
    "keep": ("Keep as is", "blue", "turning at the ideal rate"),
    "new": ("Too new", "cyan", "only the last 3 months qualify; watch"),
    "hold": ("Hold", "yellow", "out of range on thin data, but its neighbouring weight ranges are healthy"),
    "phase_out": ("Phase out", "red", "out of range on thin data, and so are its neighbours"),
    "stock_up": ("Sells when stocked", "purple", "rarely in stock, but sells fast when it is"),
    "no_history": ("Not enough history", "secondary", "too few stock snapshots to judge"),
}


def karat(purity):
    """58.3 -> '14KT', 75 -> '18KT'; 0 or missing -> '' (loose diamonds)."""
    p = pd.to_numeric(pd.Series(purity), errors="coerce").fillna(0)
    kt = (p * 24 / 100).round().astype(int)
    return pd.Series(np.where(p > 0, kt.astype(str) + "KT", ""), index=p.index)


def weight_band(weights, edges, unit="ct"):
    """Band labels and their order: 0.3 with edges [0.1, 0.25, 0.5] -> ('0.25–0.5ct', 2)."""
    edges = [float(e) for e in edges]
    labels = [f"< {edges[0]:g}{unit}"] + [f"{a:g}–{b:g}{unit}" for a, b in zip(edges[:-1], edges[1:])] \
        + [f"{edges[-1]:g}{unit} +"]
    w = pd.to_numeric(pd.Series(weights), errors="coerce").abs().fillna(0)
    order = np.searchsorted(edges, w.values, side="right")
    return pd.Series(np.array(labels)[order], index=w.index), pd.Series(order, index=w.index)


def add_brick(df, rules):
    """Adds the brick columns, weight_band/band_order and 'brick' (the display key) to stock or sales rows."""
    out = df.copy()
    no_metal = pd.to_numeric(out["purity"], errors="coerce").fillna(0).values <= 0
    out["purity"] = karat(out["purity"]).values
    if rules["turn_weight_basis"] == "diamond_ct":
        # loose diamonds carry their carats in gross weight (diamond weight is 0 and there is no metal)
        dia = pd.to_numeric(out["dia_wt"], errors="coerce").fillna(0)
        weight, unit = dia.where((dia != 0) | ~no_metal, out["gross_wt"]), "ct"
    else:
        weight, unit = out["gross_wt"], "g"
    out["weight_band"], out["band_order"] = weight_band(weight, rules["turn_weight_edges"], unit)
    for c in ("item_name", "category", "sub_category"):
        out[c] = out[c].fillna("").astype(str).str.strip()
    levels = [lv for lv in LEVELS if lv in rules["turn_brick_levels"]]
    out["brick"] = out[levels].astype(str).agg(lambda r: " · ".join(v for v in r if v), axis=1)
    return out


def fortnight_grid(as_of, dates, periods=WINDOWS["12m"]):
    """[(period_end, snapshot date or None)] going back from as_of: the latest snapshot in each fortnight."""
    as_of = pd.Timestamp(as_of)
    dates = sorted(pd.to_datetime(pd.Series(list(dates))).tolist())
    grid = []
    for k in range(periods):
        end = as_of - pd.Timedelta(days=FORTNIGHT * k)
        start = end - pd.Timedelta(days=FORTNIGHT - 1)
        inside = [d for d in dates if start <= d <= end]
        grid.append((end, inside[-1] if inside else None))
    return grid


def _weights(pattern, rules):
    """(w3, w6, w12) for a set of qualifying windows, or None."""
    if pattern == {"3m", "6m", "12m"}:
        w = np.array([rules["turn_w3_all"], rules["turn_w6_all"], rules["turn_w12_all"]], dtype=float)
    elif pattern == {"6m", "12m"}:
        w = np.array([0, rules["turn_w6_of_6_12"], 100 - rules["turn_w6_of_6_12"]], dtype=float)
    elif pattern == {"3m", "6m"}:
        w = np.array([rules["turn_w3_of_3_6"], 100 - rules["turn_w3_of_3_6"], 0], dtype=float)
    else:
        return None
    return w / w.sum() if w.sum() > 0 else None


def compute(stock, sales, as_of, rules, keys=("brick",)):
    """One row per brick (per keys, e.g. ("brick", "branch")).

    stock: brick rows (add_brick) for the grid's snapshot dates, with snapshot_date, pcs, value.
    sales: brick rows of net sale lines (returns negative) over the last 12 months: sale_date, pcs.
    """
    keys = list(keys)
    stock = stock.assign(snapshot_date=pd.to_datetime(stock["snapshot_date"]))
    sales = sales.assign(sale_date=pd.to_datetime(sales["sale_date"]))
    attrs =["item_class_group", "purity", "item_name", "category", "sub_category", "weight_band", "band_order"]
    as_of = pd.Timestamp(as_of)
    grid = fortnight_grid(as_of, stock["snapshot_date"].unique())

    pcs = stock.pivot_table(index=keys, columns="snapshot_date", values="pcs", aggfunc="sum", fill_value=0)
    sold_rows = sales[(sales["sale_date"] > as_of - pd.Timedelta(days=FORTNIGHT * WINDOWS["12m"]))
                      & (sales["sale_date"] <= as_of)]
    index = pcs.index.union(sold_rows.groupby(keys).size().index)          # full outer: dead stock and lost sales
    pcs = pcs.reindex(index, fill_value=0)
    info = pd.concat([stock[keys + attrs], sales[keys + attrs]]).drop_duplicates(keys).set_index(keys).reindex(index)

    latest = stock[stock["snapshot_date"] == stock["snapshot_date"].max()]
    now = latest.groupby(keys).agg(current_pcs=("pcs", "sum"), current_value=("value", "sum")).reindex(index, fill_value=0)
    sold_value = sold_rows[sold_rows["pcs"] > 0].groupby(keys).agg(v=("value", "sum"), p=("pcs", "sum"))
    out = pd.concat([info, now], axis=1)
    out["sold_piece_value"] = (sold_value["v"] / sold_value["p"]).reindex(index)
    avail_need, hist_need = rules["turn_availability_pct"] / 100, rules["turn_history_pct"] / 100

    for name, n in WINDOWS.items():
        cols = [snap for _, snap in grid[:n] if snap is not None and snap in pcs.columns]
        held = pcs[cols] if cols else pd.DataFrame(index=index)
        days = FORTNIGHT * n
        in_window = sold_rows[sold_rows["sale_date"] > as_of - pd.Timedelta(days=days)]
        sold = in_window.groupby(keys)["pcs"].sum().reindex(index, fill_value=0)
        out[f"data_{name}"] = len(cols)
        out[f"avail_{name}"] = (held > 0).sum(axis=1) if cols else 0
        out[f"avg_pcs_{name}"] = held.mean(axis=1) if cols else np.nan
        out[f"sold_{name}"] = sold
        out[f"annual_{name}"] = sold * 365 / days
        avg = out[f"avg_pcs_{name}"]
        out[f"turn_{name}"] = np.where(avg > 0, out[f"annual_{name}"] / avg.where(avg > 0),
                                       np.where(out[f"annual_{name}"] > 0, np.inf, np.nan))
        out[f"q_{name}"] = (len(cols) >= hist_need * n) & (out[f"avail_{name}"] >= avail_need * max(len(cols), 1))

    ideal, band = float(rules["target_turn"]), rules["turn_band_pct"] / 100
    rows = []
    for key, r in out.iterrows():
        pattern = {w for w in WINDOWS if r[f"q_{w}"]}
        w = _weights(pattern, rules)
        rec = {"pattern": "+".join(x for x in WINDOWS if x in pattern) or "none", "weights": None,
               "blended_turn": np.nan, "annual_sales": np.nan}
        if w is not None:
            turns = np.array([r[f"turn_{x}"] for x in WINDOWS], dtype=float)
            annual = np.array([r[f"annual_{x}"] for x in WINDOWS], dtype=float)
            used = w > 0
            rec["weights"] = "/".join(f"{100 * v:.0f}" for v in w)
            rec["blended_turn"] = float(np.dot(w[used], turns[used]))
            rec["annual_sales"] = float(np.dot(w[used], annual[used]))
            rec["status"] = _range(rec["blended_turn"], ideal, band)
        elif pattern == {"3m"}:
            rec["status"], rec["annual_sales"] = "new", r["annual_3m"]
        else:
            longest = next((x for x in ("12m", "6m", "3m") if r[f"data_{x}"] >= hist_need * WINDOWS[x]), None)
            if longest is None:
                rec["status"] = "no_history"
            else:
                rec["blended_turn"], rec["annual_sales"] = r[f"turn_{longest}"], r[f"annual_{longest}"]
                within = _range(rec["blended_turn"], ideal, band) if not np.isnan(rec["blended_turn"]) else "reduce"
                rec["status"] = "keep" if within == "keep" else f"check_{within}"
        rows.append(rec)
    out = pd.concat([out, pd.DataFrame(rows, index=out.index)], axis=1)
    out = out[(out["current_pcs"] > 0) | (out["sold_12m"] > 0)]
    out = _neighbour_check(out.reset_index(), keys)
    out["ideal_turn"] = ideal
    out["ideal_pcs"] = np.where(out["annual_sales"].notna() & (out["annual_sales"] > 0),
                                np.round(out["annual_sales"].clip(lower=0) / ideal), 0)
    out.loc[out["status"].isin(["new", "no_history", "hold"]), "ideal_pcs"] = out["current_pcs"]
    out["change_pcs"] = out["ideal_pcs"] - out["current_pcs"]
    out["avg_piece_value"] = np.where(out["current_pcs"] > 0, out["current_value"] / out["current_pcs"].clip(lower=1),
                                      out["sold_piece_value"])          # nothing held: what its sales were worth
    return out.drop(columns="sold_piece_value")


def _range(turn, ideal, band):
    if turn > ideal * (1 + band):
        return "increase"
    if turn < ideal * (1 - band):
        return "reduce"
    return "keep"


def _neighbour_check(t, keys):
    """Resolve not-qualified, out-of-range bricks by their neighbouring weight ranges (one band down and up)."""
    t = t.copy()
    t["band_order"] = pd.to_numeric(t["band_order"], errors="coerce").fillna(-99).astype(int)
    others = [k for k in keys if k != "brick"] + ["item_class_group", "purity", "item_name", "category", "sub_category"]
    sig = t[others].astype(str).agg("|".join, axis=1) + "#" + t["band_order"].astype(str)
    present_all, healthy = set(sig), set(sig[t["status"] == "keep"])
    for i in t.index[t["status"].str.startswith("check_")]:
        base, b = sig[i].rsplit("#", 1)[0], int(t.at[i, "band_order"])
        present = [n for n in (f"{base}#{b - 1}", f"{base}#{b + 1}") if n in present_all]
        if present and all(n in healthy for n in present):
            t.at[i, "status"] = "hold"
        else:
            t.at[i, "status"] = "stock_up" if t.at[i, "status"] == "check_increase" else "phase_out"
    return t
