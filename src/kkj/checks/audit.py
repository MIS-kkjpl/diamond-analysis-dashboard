"""
Accuracy audit of every stock and sales row held, June 2025 onwards. Reads only; no ERP calls (voucher
histories are fetched beforehand by kkj.pipelines.label_vouchers).

The ERP is the source of truth. Tolerances are EXACT: counts equal, sums equal to the ERP's own
totals at the precision the ERP reports them (3 decimals for weights and pieces, 2 for rupees).

  Layer 1  database vs ERP       every load re-checked against the totals the ERP sent with it;
                                 gaps, duplicates, impossible values
  Layer 2  sources vs each other same-date comparisons: ERP as-on vs ERP daily, legacy file vs ERP as-on
  Layer 3  stock-flow continuity between consecutive snapshots every label that leaves is sold
                                 (or comes back), every label that arrives is new, returned or back
  Layer 4  the ERP's own record  for every movement our data can't explain, the voucher on the label's ERP
                                 history that does (erp.label_voucher, kkj.pipelines.label_vouchers); and every
                                 location and voucher type is on the lists and classified (Settings)

Run:  python -m kkj.checks.audit            -> data/audit/audit_<stamp>.xlsx (summary + one sheet per
                                               exception list) and .md (the summary)
"""
import re
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import text

from kkj import settings

AUDIT_FROM = date(2025, 6, 1)
PURITIES = {0.0, 37.5, 58.3, 75.0, 91.6, 99.5, 100.0}
NO_DIAMOND_OK = {"LOOSE DIAMOND", "Customer Diamond", "Broken Dia Jew"}   # weight held elsewhere / not ours
SOURCE_RANK = {"erp": 1, "erp_asof": 2, "csv_legacy": 3}
SOURCE_NAME = {"erp": "ERP daily", "erp_asof": "ERP as-on", "csv_legacy": "Legacy file"}


class Audit:
    def __init__(self):
        self.results, self.exceptions = [], {}

    def check(self, layer, name, scope, expected, actual, passed=None, note=""):
        passed = (expected == actual) if passed is None else bool(passed)
        self.results.append({"layer": layer, "check": name, "scope": str(scope), "expected": expected,
                             "actual": actual, "result": "PASS" if passed else "FAIL", "note": note})
        return passed

    def info(self, layer, name, scope, actual, note=""):
        self.results.append({"layer": layer, "check": name, "scope": str(scope), "expected": "",
                             "actual": actual, "result": "INFO", "note": note})

    def keep(self, sheet, df):
        if df is not None and len(df):
            self.exceptions[sheet] = pd.concat([self.exceptions.get(sheet), df]) if sheet in self.exceptions else df


def _read(conn, sql, **p):
    return pd.read_sql(text(sql), conn, params=p)


def _num(s):
    """'2,376,412,510.96 vs 2,376,412,510.96' -> (2376412510.96, 2376412510.96)."""
    a, b = [float(x.replace(",", "")) for x in re.findall(r"-?[\d,]+\.?\d*", s)[:2]]
    return a, b


def _erp_totals(checks, prefix):
    """{'pcs': 10182.0, 'item_amt': ...} from the load's own check list: the ERP side of 'Sum x == <prefix>'."""
    out = {}
    for c in checks or []:
        m = re.match(r"(?:Rows|Row count) == (?:step A recordCount|Total \(N\))", c["check"])
        if m:
            out["rows"] = _num(c["detail"])[1]
        m = re.match(r"Sum (\w+) == " + prefix, c["check"])
        if m:
            out[m.group(1)] = _num(c["detail"])[1]
    return out


# ── Layer 1: database vs ERP ───────────────────────────────────────────────────
STOCK_SUMS = {"pcs": 3, "gross_wt": 3, "net_wt": 3, "dia_pcs": 3, "dia_wt": 3, "item_amt": 2}


def _compare_sums(a, layer, name, scope, db_row, erp):
    ok = a.check(layer, f"{name}: rows", scope, int(erp.get("rows", -1)), int(db_row["n"]))
    for col, dec in STOCK_SUMS.items():
        if col in erp:
            ok &= a.check(layer, f"{name}: sum {col}", scope, round(erp[col], dec), round(float(db_row[col] or 0), dec))
    return ok


def stock_loads(a, conn):
    sums = ", ".join(f"sum({c}) AS {c}" for c in STOCK_SUMS)
    daily = _read(conn, f"SELECT snapshot_date, run_id, count(*) AS n, {sums} FROM erp.stock_snapshot GROUP BY 1, 2")
    runs = _read(conn, "SELECT run_id, validation FROM erp.etl_run_log").set_index("run_id")["validation"]
    for _, r in daily.iterrows():
        _compare_sums(a, 1, "ERP daily stock vs ERP totals", r["snapshot_date"], r, _erp_totals(runs.get(r["run_id"]), "totals row"))
    dup = daily[daily.duplicated("snapshot_date", keep=False)]
    a.check(1, "ERP daily stock: one load per date", "all dates", 0, len(dup))

    hist = _read(conn, f"SELECT snapshot_date, source, count(*) AS n, {sums} FROM stock_hist.snapshot GROUP BY 1, 2")
    status = _read(conn, "SELECT snapshot_date, source, status, labels, checks FROM stock_hist.snapshot_status")
    for _, s in status.iterrows():
        row = hist[(hist["snapshot_date"] == s["snapshot_date"]) & (hist["source"] == s["source"])]
        scope = f"{s['snapshot_date']} {SOURCE_NAME[s['source']]}"
        if s["status"] != "loaded":
            a.check(1, "Set-aside snapshot holds no rows", scope, 0, int(row["n"].sum()))
            continue
        if row.empty:
            a.check(1, "Loaded snapshot has rows", scope, int(s["labels"]), 0)
            continue
        if s["source"] == "erp_asof":
            _compare_sums(a, 1, "ERP as-on stock vs ERP totals", scope, row.iloc[0],
                          _erp_totals((s["checks"] or {}).get("file"), "totals row"))
        else:   # the legacy files carry no totals: their diamond label count when imported
            a.check(1, "Legacy stock: rows vs labels in the file", scope, int(s["labels"]), int(row.iloc[0]["n"]))


SALES_SUMS = {"pcs": 3, "item_amt": 2, "net_wt": 3, "dia_wt": 3}


def sales_loads(a, conn):
    sums = ", ".join(f"sum({c}) AS {c}" for c in SALES_SUMS)
    held = _read(conn, f"SELECT load_id, count(*) AS n, {sums} FROM sales.sales_line GROUP BY 1").set_index("load_id")
    loads = _read(conn, "SELECT load_id, source, date_from, date_to, status, record_count, validation FROM sales.load_log")
    for _, ld in loads.iterrows():
        if ld["load_id"] not in held.index:
            continue
        r = held.loc[ld["load_id"]]
        scope = f"load {ld['load_id']} {ld['source']} {ld['date_from']}..{ld['date_to']}"
        if ld["source"] == "csv_legacy":
            a.check(1, "Legacy sales: rows vs lines imported", scope, int(ld["record_count"]), int(r["n"]))
            continue
        later = loads[(loads["load_id"] > ld["load_id"]) & (loads["status"] == "success") & (loads["source"] == "erp")
                      & (loads["date_from"] <= ld["date_to"]) & (loads["date_to"] >= ld["date_from"])]
        if len(later):          # its rows for the overlapping dates were replaced; the later load is checked itself
            a.info(1, "ERP sales load partly replaced by a later load", scope, int(r["n"]),
                   note=f"rows still held; replaced by load {', '.join(map(str, later['load_id']))}, which is checked on its own")
            continue
        erp = {}
        for c in ld["validation"] or []:
            m = re.match(r"Sum (\w+) == serverTotal", c["check"])
            if m:
                erp[m.group(1)] = _num(c["detail"])[1]
            if c["check"].startswith("Rows == step A recordCount"):
                erp["rows"] = _num(c["detail"])[1]
        a.check(1, "ERP sales vs ERP totals: rows", scope, int(erp.get("rows", -1)), int(r["n"]))
        for col, dec in SALES_SUMS.items():
            if col in erp:
                a.check(1, f"ERP sales vs ERP totals: sum {col}", scope, round(erp[col], dec), round(float(r[col] or 0), dec))


def coverage(a, conn, today):
    dates = _read(conn, """SELECT DISTINCT snapshot_date, 'erp' AS source FROM erp.stock_snapshot
                           UNION SELECT snapshot_date, source FROM stock_hist.snapshot_status WHERE status = 'loaded'""")
    dates["snapshot_date"] = pd.to_datetime(dates["snapshot_date"])
    best = dates.assign(rank=dates["source"].map(SOURCE_RANK)).sort_values("rank").drop_duplicates("snapshot_date")
    ends = pd.date_range(pd.Timestamp(today), pd.Timestamp(AUDIT_FROM), freq="-14D")
    gaps = []
    for end in ends:
        inside = best[(best["snapshot_date"] > end - pd.Timedelta(days=14)) & (best["snapshot_date"] <= end)]
        if inside.empty:
            gaps.append({"fortnight_ending": end.date(), "fortnight_from": (end - pd.Timedelta(days=13)).date()})
    gaps = pd.DataFrame(gaps)
    a.check(1, "Every fortnight has a usable stock snapshot", f"{AUDIT_FROM} to {today}", 0, len(gaps),
            note="missing fortnights can be filled with ERP as-on pulls")
    a.keep("L1 stock fortnights missing", gaps)

    daily = pd.to_datetime(dates.loc[dates["source"] == "erp", "snapshot_date"])
    if len(daily):
        days = pd.bdate_range(daily.min(), pd.Timestamp(today), freq="C", weekmask="Mon Tue Wed Thu Fri Sat")
        asof = pd.to_datetime(dates.loc[dates["source"] == "erp_asof", "snapshot_date"])
        missing = sorted(set(days.date) - set(daily.dt.date) - set(asof.dt.date))   # a missed day can be pulled as-on
        filled = sorted((set(days.date) - set(daily.dt.date)) & set(asof.dt.date))
        a.check(1, "Stock on every working day (Mon–Sat) since the first daily load, daily or as-on",
                f"{daily.min().date()} to {today}", 0, len(missing),
                note=(", ".join(map(str, missing)) or "none missing")
                     + (f"; {len(filled)} day(s) filled from ERP as-on" if filled else ""))

    s = _read(conn, """SELECT source, date_trunc('month', sale_date)::date AS month, count(*) AS lines, max(sale_date) AS last
                       FROM sales.v_sales_line WHERE sale_date >= :d GROUP BY 1, 2 ORDER BY 2""", d=AUDIT_FROM)
    a.info(1, "Sales months held (view)", "by month", "; ".join(f"{r.month:%b %y} {r.source} {r.lines}" for r in s.itertuples()))
    last = _read(conn, "SELECT max(sale_date) AS d FROM sales.v_sales_line")["d"].iloc[0]
    stock_last = daily.max().date() if len(daily) else None
    a.check(1, "Sales loaded up to the latest stock date", f"stock {stock_last}", str(stock_last), str(last),
            passed=last is not None and stock_last is not None and last >= stock_last - pd.Timedelta(days=1).to_pytimedelta())
    loads = _read(conn, """SELECT (started_at AT TIME ZONE 'Asia/Kolkata') AS started, date_from, date_to
                           FROM sales.load_log WHERE status = 'success' AND source = 'erp'""")
    early = []
    for d in pd.date_range(AUDIT_FROM, pd.Timestamp(today) - pd.Timedelta(days=1)):
        cov = loads[(pd.to_datetime(loads["date_from"]) <= d) & (pd.to_datetime(loads["date_to"]) >= d)]
        if len(cov) and pd.to_datetime(cov["started"]).max().normalize() <= d:
            early.append(d.date())
    a.check(1, "Every past sale day was last pulled after the day ended", f"{AUDIT_FROM} to yesterday", 0, len(early),
            note=", ".join(map(str, early)) or "all re-pulled after the day")
    months = pd.period_range(AUDIT_FROM, today, freq="M")
    held = set(pd.to_datetime(s["month"]).dt.to_period("M"))
    a.check(1, "Every month has sales lines", f"{AUDIT_FROM:%b %Y} to {today:%b %Y}", 0,
            len([m for m in months if m not in held]), note=", ".join(str(m) for m in months if m not in held))


def stock_rows(conn):
    """Every stock row since AUDIT_FROM from every source, with the fields the checks need."""
    cols = """snapshot_date, barcode, branch, location, item_class_group, purity, pcs, gross_wt, net_wt, dia_wt, item_amt,
              taxable_amount, coalesce(label_dt, labeling_date) AS label_date"""
    df = _read(conn, f"""SELECT 'erp' AS source, {cols} FROM erp.stock_snapshot
                         UNION ALL SELECT source, {cols} FROM stock_hist.snapshot WHERE snapshot_date >= :d""", d=AUDIT_FROM)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df["label_date"] = pd.to_datetime(df["label_date"])
    return df


def stock_fields(a, st):
    by = ["snapshot_date", "source"]
    dup = st[st.duplicated(by + ["barcode"], keep=False)]
    a.check(1, "Stock: a barcode appears once per snapshot", "all snapshots", 0, len(dup))
    a.keep("L1 stock duplicate barcodes", dup)
    tests = {
        "barcode missing": st["barcode"].isna(),
        "purity not a known karat": ~st["purity"].round(1).isin(PURITIES),
        "pieces zero or negative": st["pcs"].fillna(0) <= 0,
        # as-on item_amt leaves out stones and metal for many labels; taxable is the full value there
        "value zero or negative (company stock)": (np.where(st["source"] == "erp_asof", st["taxable_amount"].fillna(0),
                                                             st["item_amt"].fillna(0)) <= 0)
                                                   & (st["item_class_group"] != "Customer Diamond"),
        "label dated after the snapshot": st["label_date"] > st["snapshot_date"],
        "branch or location missing": st["branch"].isna() | st["location"].isna(),
        "diamond piece without diamond weight": (st["dia_wt"].fillna(0) <= 0) & ~st["item_class_group"].isin(NO_DIAMOND_OK),
    }
    latest = st["snapshot_date"] == st["snapshot_date"].max()
    for name, mask in tests.items():
        n_all, n_now = int(mask.sum()), int((mask & latest).sum())
        facts = {"diamond piece without diamond weight": "ERP data, not a load error",
                 "purity not a known karat": "ERP records these purities (e.g. 22.75, 24)",
                 "branch or location missing": "blank in the legacy files themselves",
                 "value zero or negative (company stock)": "ERP data: mostly loose diamonds valued at 0 in the export"}
        hard = name not in facts
        by_src = mask.groupby(st["source"]).sum()
        where = ", ".join(f"{SOURCE_NAME[k]} {int(v)}" for k, v in by_src.items() if v)
        if hard:
            a.check(1, f"Stock: {name}", "all snapshots", 0, n_all, note=f"{n_now} in the latest snapshot; {where}")
        else:
            a.info(1, f"Stock: {name}", "all snapshots", n_all, note=f"{n_now} in the latest snapshot; {where}. {facts[name]}")
        a.keep(f"L1 stock {name}"[:31], st.loc[mask & latest] if not hard else st.loc[mask])


def sales_rows(conn):
    df = _read(conn, """SELECT v.source, v.sale_date, v.bill_no, v.barcode, v.branch, v.item_class_group, v.purity, v.pcs,
                               coalesce(v.label_dt, v.labeling_date) AS label_date,
                               v.item_amt, v.taxable_amount, v.inventory_ref_code, v.orig_bill_no, v.orig_sale_date,
                               o.barcode AS orig_barcode
                        FROM sales.v_sales_line v
                        LEFT JOIN sales.v_sales_line o ON v.pcs < 0 AND o.pcs > 0 AND o.bill_no = v.orig_bill_no
                             AND o.inventory_ref_code = v.inventory_ref_code
                        WHERE v.sale_date >= :d""", d=AUDIT_FROM)
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


def sales_fields(a, sa, today):
    sale, ret = sa[sa["pcs"] > 0], sa[sa["pcs"] < 0]
    erp_sale = sale[sale["source"] == "erp"]
    disc = 1 - erp_sale["taxable_amount"] / erp_sale["item_amt"]
    keys = sa[sa["source"] == "erp"]
    tests = {
        "sale dated in the future": sa["sale_date"] > pd.Timestamp(today),
        "sale without a barcode": sale["barcode"].isna(),
        "sale value zero or negative": sale["item_amt"].fillna(0) <= 0,
        "ERP sale without taxable amount": erp_sale["taxable_amount"].isna(),
        "ERP discount below 0% or above 50%": (disc < -0.0001) | (disc > 0.5),
        "ERP return not linked to its sale": (ret["source"] == "erp") & ret["orig_barcode"].isna(),
        "ERP line key duplicated (bill + inventory ref)": keys.duplicated(["bill_no", "inventory_ref_code"], keep=False),
    }
    frames = {"ERP discount below 0% or above 50%": erp_sale, "ERP sale without taxable amount": erp_sale,
              "sale without a barcode": sale, "sale value zero or negative": sale,
              "ERP return not linked to its sale": ret, "ERP line key duplicated (bill + inventory ref)": keys}
    for name, mask in tests.items():
        df = frames.get(name, sa)
        bad = df.loc[mask[mask].index]
        info = name.startswith("ERP discount")          # an ERP fact to review, not a load error
        if info:
            a.info(1, f"Sales: {name}", "all lines", len(bad), note="ERP data; review the bills")
        else:
            a.check(1, f"Sales: {name}", "all lines", 0, len(bad))
        a.keep(f"L1 sales {name}"[:31], bad)


# ── Layer 2: the sources against each other on the same date ─────────────────
def same_dates(a, st, sa, v=None):
    sold = sa[sa["pcs"] > 0][["barcode", "sale_date"]].dropna()
    voucher_days = set() if v is None or not len(v) else set(zip(v["barcode"], v["doc_date"]))
    srcs = st.groupby("snapshot_date")["source"].unique()
    for d, s in srcs.items():
        s = set(s)
        for x, y in (("erp_asof", "erp"), ("csv_legacy", "erp_asof")):
            if {x, y} <= s:
                A = st[(st["snapshot_date"] == d) & (st["source"] == x)].set_index("barcode")
                B = st[(st["snapshot_date"] == d) & (st["source"] == y)].set_index("barcode")
                only_a, only_b = A.index.difference(B.index), B.index.difference(A.index)
                both = A.index.intersection(B.index)
                scope = f"{d.date()} {SOURCE_NAME[x]} vs {SOURCE_NAME[y]}"
                if y == "erp":      # ERP daily is the mid-day position, as-on the end of the day
                    sold_today = set(sold.loc[sold["sale_date"] == d, "barcode"])
                    new_ok = A.loc[only_a, "label_date"] == d                          # labelled that afternoon
                    gone_ok = pd.Index(only_b).isin(list(sold_today))                   # sold that afternoon
                    # any other ERP voucher that day (transfer, booking, repair, set) also moves a label mid-day
                    new_v = pd.Series([(b, d) in voucher_days for b in only_a], index=only_a)
                    gone_v = pd.Series([(b, d) in voucher_days for b in only_b], index=only_b)
                    unexplained = int((~(new_ok.values | new_v.values)).sum() + (~(gone_ok | gone_v.values)).sum())
                    a.check(2, "Labels differing within the day moved in the ERP that day", scope, 0, unexplained,
                            note=f"{len(only_a)} only end-of-day ({int(new_ok.sum())} labelled that day, "
                                 f"{int((new_v.values & ~new_ok.values).sum())} other voucher that day), "
                                 f"{len(only_b)} only mid-day ({int(gone_ok.sum())} sold that day, "
                                 f"{int((gone_v.values & ~gone_ok).sum())} other voucher that day)")
                else:               # legacy file vs ERP as-on: the app uses the ERP figure, the file is only a cross-check
                    a.info(2, "Same labels in both", scope, len(only_a) + len(only_b),
                           note=f"{len(only_a)} only in {SOURCE_NAME[x]}, {len(only_b)} only in {SOURCE_NAME[y]} "
                                f"(of {len(both):,} in both; the app uses {SOURCE_NAME[y]})")
                diff_rows = []
                for col in ("pcs", "gross_wt", "net_wt", "dia_wt", "branch", "location"):
                    va, vb = A.loc[both, col], B.loc[both, col]
                    neq = (va.round(3) != vb.round(3)) if va.dtype.kind == "f" else (va.fillna("") != vb.fillna(""))
                    if y == "erp" and col in ("branch", "location"):
                        a.info(2, f"Moved {col} during the day", scope, int(neq.sum()),
                               note="mid-day vs end-of-day position (e.g. dispatched from Head Office to branches)")
                    elif x == "csv_legacy":
                        a.info(2, f"Same {col} on matching labels", scope, int(neq.sum()),
                               note=f"of {len(both):,} labels; the app uses {SOURCE_NAME[y]}")
                    else:
                        a.check(2, f"Same {col} on matching labels", scope, 0, int(neq.sum()))
                    if neq.any():
                        diff_rows.append(pd.DataFrame({"date": d.date(), "compare": scope, "barcode": neq[neq].index,
                                                       "field": col, x: va[neq].values, y: vb[neq].values}))
                a.keep("L2 field differences", pd.concat(diff_rows) if diff_rows else None)
                extra = pd.concat([A.loc[only_a].assign(only_in=SOURCE_NAME[x]), B.loc[only_b].assign(only_in=SOURCE_NAME[y])])
                a.keep("L2 labels in one source only", extra.reset_index().assign(compare=scope))


# ── Layer 3: stock-flow continuity ────────────────────────────────────────────
def flow_events(st, sa, out_locations=()):
    """Every label that left or arrived between consecutive snapshots (each date from its best source), with why as
    far as our own data tells. left: sold / back in a later snapshot / delabelled (left from a location that does
    not count as stock) / UNEXPLAINED. arrived: newly labelled / sales return / back after an absence / UNEXPLAINED.
    never: every sale of a label not held in stock before it (between = labelled after the last snapshot before
    the sale). Returns (left, arrived, never, chain); chain = the stock rows used, one source per date."""
    best = st.assign(rank=st["source"].map(SOURCE_RANK)).sort_values("rank")
    first_src = best.drop_duplicates("snapshot_date")[["snapshot_date", "source"]]
    chain = best.merge(first_src, on=["snapshot_date", "source"])
    dates = sorted(chain["snapshot_date"].unique())
    by_date = {d: chain[chain["snapshot_date"] == d].set_index("barcode") for d in dates}
    ever_seen = chain.groupby("barcode")["snapshot_date"].agg(["min", "max"])
    sold = sa[sa["pcs"] > 0][["barcode", "sale_date"]].dropna()
    returned = sa[sa["pcs"] < 0].assign(barcode=lambda d: d["orig_barcode"])[["barcode", "sale_date"]].dropna()
    left, arrived = [], []
    for d0, d1 in zip(dates[:-1], dates[1:]):
        A, B = by_date[d0], by_date[d1]
        gone, new = A.index.difference(B.index), B.index.difference(A.index)
        # a sale on d0 after a mid-day snapshot still removes the label by d1, so the window includes d0
        sold_in = set(sold.loc[(sold["sale_date"] >= d0) & (sold["sale_date"] <= d1), "barcode"])
        back_in = set(returned.loc[(returned["sale_date"] >= d0) & (returned["sale_date"] <= d1), "barcode"])
        g = A.loc[gone, ["branch", "location", "item_class_group", "item_amt"]].copy()
        g["why"] = np.select([g.index.isin(list(sold_in)), ever_seen.reindex(g.index)["max"] > d1,
                              g["location"].isin(list(out_locations))],
                             ["sold", "back in a later snapshot", "delabelled"], "UNEXPLAINED")
        left.append(g.assign(last_seen=d0, missing_from=d1))
        n = B.loc[new, ["branch", "location", "item_class_group", "label_date", "item_amt"]].copy()
        n["why"] = np.select([(n["label_date"] >= d0) & (n["label_date"] <= d1), n.index.isin(list(back_in)),
                              ever_seen.reindex(n.index)["min"] < d0],
                             ["newly labelled", "sales return", "back after an absence"], "UNEXPLAINED")
        arrived.append(n.assign(not_in=d0, first_seen=d1))
    left = pd.concat(left).rename_axis("barcode").reset_index()
    arrived = pd.concat(arrived).rename_axis("barcode").reset_index()
    lines = sa[(sa["pcs"] > 0) & sa["barcode"].notna() & (sa["sale_date"] >= pd.Timestamp(dates[0]))]
    held_before = lines.merge(ever_seen, left_on="barcode", right_index=True, how="left")
    never = held_before[held_before["min"].isna() | (held_before["min"] > held_before["sale_date"])].copy()
    snaps = pd.Series(dates)
    never["prev_snapshot"] = never["sale_date"].map(lambda d: snaps[snaps <= d].max())
    never["label_date"] = pd.to_datetime(never["label_date"])
    never["between"] = never["label_date"] >= never["prev_snapshot"]
    never = never.drop(columns=["min", "max"], errors="ignore")
    never.attrs["lines"] = len(lines)
    return left, arrived, never, chain


def stock_flow(a, st, sa, kinds, out_locations=()):
    left, arrived, never, chain = flow_events(st, sa, out_locations)
    dates = sorted(chain["snapshot_date"].unique())
    left["showroom"] = (left["branch"].map(kinds) == "showroom").values
    branch_at = {d: chain[chain["snapshot_date"] == d].set_index("barcode")["branch"] for d in dates}
    rows = []
    for d0, d1 in zip(dates[:-1], dates[1:]):
        g, n = left[left["missing_from"] == d1], arrived[arrived["first_seen"] == d1]
        common = branch_at[d0].index.intersection(branch_at[d1].index)
        un = g["why"] == "UNEXPLAINED"
        rows.append({"from": d0.date(), "to": d1.date(), "days": (d1 - d0).days,
                     "labels_from": len(branch_at[d0]), "labels_to": len(branch_at[d1]), "left": len(g),
                     "left_sold": int((g["why"] == "sold").sum()), "left_delabelled": int((g["why"] == "delabelled").sum()),
                     "left_unexplained": int(un.sum()), "left_showroom": int(g["showroom"].sum()),
                     "left_unexplained_showroom": int((un & g["showroom"]).sum()),
                     "arrived": len(n), "arrived_new": int((n["why"] == "newly labelled").sum()),
                     "arrived_unexplained": int((n["why"] == "UNEXPLAINED").sum()),
                     "branch_moves": int((branch_at[d0].loc[common] != branch_at[d1].loc[common]).sum())})
    s = pd.DataFrame(rows)
    a.keep("L3 flow by interval", s)
    gone = left[left["why"] == "UNEXPLAINED"]
    a.keep("L3 left, unexplained", gone[["barcode", "branch", "location", "item_class_group", "item_amt"]].assign(
        last_seen=gone["last_seen"].dt.date, missing_from=gone["missing_from"].dt.date))
    new = arrived[arrived["why"] == "UNEXPLAINED"]
    a.keep("L3 arrived, unexplained", new[["barcode", "branch", "location", "item_class_group", "label_date", "item_amt"]].assign(
        first_seen=new["first_seen"].dt.date, not_in=new["not_in"].dt.date))
    span = f"{len(s)} intervals, {s['from'].min()} to {s['to'].max()}"
    t, b = int(s["left_showroom"].sum()), int(s["left_unexplained_showroom"].sum())
    a.info(3, "Labels that left a showroom without a sale in our data (layer 4 explains them)", span, b,
            note=f"{t:,} left showrooms; {100 * (1 - b / max(t, 1)):.2f}% explained")
    t, b = int(s["left"].sum() - s["left_showroom"].sum()), int(s["left_unexplained"].sum() - s["left_unexplained_showroom"].sum())
    a.info(3, "Labels that left Head Office / wholesale / other without a retail sale", span, b,
           note=f"of {t:,} that left, {int(s['left_delabelled'].sum()):,} were delabelled at HO MD COUNTER (Stock Outward "
                "MHODDJ / MHODB); the rest are likely wholesale sales, returns to suppliers and melting")
    t, b = int(s["arrived"].sum()), int(s["arrived_unexplained"].sum())
    a.info(3, "Labels that arrived not new, returned or back in our data (layer 4 explains them)", span, b,
            note=f"{t:,} arrived; {100 * (1 - b / max(t, 1)):.2f}% explained")
    rest = never[~never["between"]]
    n_lines = never.attrs.get("lines", 0)
    a.info(3, "Sales of labels not held in stock before them (nor labelled since the last snapshot)",
           f"sales since {pd.Timestamp(dates[0]).date()}", len(rest),
            note=f"{n_lines:,} sales; {len(never) - len(rest)} made and sold between snapshots; "
                 f"{100 * (1 - len(rest) / max(n_lines, 1)):.1f}% matched")
    a.keep("L3 sold, never in stock", rest.drop(columns=["between"]))
    return left, arrived, never


def voucher_data(conn):
    """ERP voucher histories (erp.label_voucher) with each voucher's effect from config.voucher_type, which labels
    were fetched, the location list and our sales bill numbers."""
    v = _read(conn, """SELECT v.barcode, v.fy, v.seq, v.doc_date, v.doc_no, v.series, v.voucher, v.tx_type, v.location,
                              nullif(v.order_no, '') AS order_no, coalesce(t1.effect, t2.effect, 'unknown') AS effect
                       FROM erp.label_voucher v
                       LEFT JOIN config.voucher_type t1 ON t1.voucher = v.voucher AND t1.series = v.series
                       LEFT JOIN config.voucher_type t2 ON t2.voucher = v.voucher AND t2.series = '*'
                       WHERE NOT coalesce(v.cancelled, false)""")
    v["doc_date"] = pd.to_datetime(v["doc_date"])
    fetched = _read(conn, "SELECT barcode, fy, fetched_at, error FROM erp.label_voucher_fetch")
    locations = _read(conn, "SELECT location, kind, counts_as_stock FROM config.location")
    bills = set(_read(conn, "SELECT DISTINCT bill_no FROM sales.v_sales_line WHERE bill_no IS NOT NULL")["bill_no"])
    return v, fetched, locations, bills


EFFECT_WHY = {"sale": "ERP sale", "booking": "booked for a customer", "outward": "Stock Outward",
              "repair_issue": "sent for repair", "approval_issue": "issued on approval", "purchase_return": "returned to supplier",
              "transfer": "transferred", "labelling": "relabelled", "booking_return": "booking released",
              "sale_return": "sales return", "inward": "Stock Inward", "repair_receipt": "back from repair",
              "approval_return": "back from approval", "purchase": "purchased", "set_combine": "combined into a set",
              "set_split": "set broken up", "relabel": "relabelled", "no_effect": "no stock effect",
              "other": "other ERP voucher"}


def _window(v, ev, d0, d1):
    """Vouchers of each event's label dated from d0 to d1 (both snapshot days included: a voucher on d0 after a
    mid-day snapshot, or on d1 before one). ev must have columns barcode, d0, d1 and an 'event' id."""
    m = ev[["event", "barcode", d0, d1]].merge(v, on="barcode")
    keep = (m["doc_date"] >= m[d0]) & (m["doc_date"] <= m[d1])
    return m[keep].sort_values(["event", "doc_date", "fy", "seq"])


def _order_open(v, ev, d1):
    """Per event: was the label a customer-order label on d1? It carries an order number on a voucher up to d1 and
    no release (booking_return, e.g. the UB pair) on or after that voucher."""
    m = ev[["event", "barcode", d1]].merge(v, on="barcode")
    m = m[m["doc_date"] <= m[d1]]
    last_order = m[m["order_no"].notna()].groupby("event")["doc_date"].max()
    last_release = m[m["effect"] == "booking_return"].groupby("event")["doc_date"].max()
    open_ = last_order.to_frame("o").join(last_release.rename("r"), how="left")
    return set(open_.index[open_["r"].isna() | (open_["o"] > open_["r"])])


def explain_left(left, v, fetched, bills, out_locations=()):
    """For each unexplained exit: the label's state in the ERP when it went missing = its last voucher in the
    interval (a relabel or set voucher pair nets out: the In that follows is the state). Out -> what took it out
    (sale, booking, Stock Outward, repair ...). In at a location that does not count as stock (HO MD COUNTER) ->
    moved there. In anywhere else -> the ERP holds it but our snapshot does not: a data gap."""
    ev = left.reset_index(drop=True).rename_axis("event").reset_index()
    w = _window(v, ev, "last_seen", "missing_from")
    w = w[w["effect"] != "no_effect"]
    last = w.groupby("event").tail(1).set_index("event")
    order_open = _order_open(v, ev, "missing_from")
    for c, to in (("voucher", "voucher"), ("doc_no", "doc_no"), ("series", "series"), ("doc_date", "doc_date"),
                  ("location", "voucher_location"), ("tx_type", "tx_type"), ("order_no", "order_no")):
        ev[to] = last[c].reindex(ev["event"]).values
    effect = last["effect"].reindex(ev["event"]).values
    seen = set(fetched.loc[fetched["error"].isna(), "barcode"])
    why = []
    for ev_id, b, e, t, dn, s_, vv, loc, o in zip(ev["event"], ev["barcode"], effect, ev["tx_type"], ev["doc_no"],
                                                ev["series"], ev["voucher"], ev["voucher_location"], ev["order_no"]):
        if pd.isna(e):
            why.append("history not fetched" if b not in seen else "no ERP voucher in the interval")
        elif e == "booking":
            why.append("booked for a customer")
        elif str(t).lower() == "in" and (pd.notna(o) or ev_id in order_open):
            why.append("customer-order label (order labels are not in our stock pull)")
        elif str(t).lower() == "in":
            why.append("moved to an out-of-stock location (HO MD COUNTER)" if loc in set(out_locations)
                       else "in ERP stock, missing from our snapshot")
        elif e == "sale":
            why.append("ERP sale, bill not in our sales" if dn not in bills else "ERP sale (in our sales on another date)")
        elif e == "outward":
            why.append(f"Stock Outward {s_}")
        elif e == "unknown":
            why.append(f"not classified: {vv} {s_}")
        else:
            why.append(EFFECT_WHY.get(e, e))
    ev["erp_why"] = why
    return ev.drop(columns="event")


def explain_arrived(arrived, v, fetched):
    """For each unexplained arrival: the ERP voucher that brought the label (back) into stock in the interval."""
    ev = arrived.reset_index(drop=True).rename_axis("event").reset_index()
    w = _window(v, ev, "not_in", "first_seen")
    prio = {"booking_return": 0, "sale_return": 1, "labelling": 2, "set_split": 3, "set_combine": 3, "relabel": 4,
            "inward": 4, "repair_receipt": 5, "approval_return": 6, "purchase": 7, "transfer": 8}
    inn = w[w["tx_type"].str.lower() == "in"].assign(p=lambda d: d["effect"].map(prio).fillna(9))
    best = inn.sort_values(["event", "p", "doc_date"]).groupby("event").head(1).set_index("event")
    for c in ("voucher", "doc_no", "series", "doc_date", "location"):
        ev["voucher" if c == "voucher" else ("voucher_location" if c == "location" else c)] = best[c].reindex(ev["event"]).values
    effect = best["effect"].reindex(ev["event"]).values
    seen = set(fetched.loc[fetched["error"].isna(), "barcode"])
    ev["erp_why"] = [("history not fetched" if b not in seen else "no ERP voucher in the interval") if pd.isna(e) else
                     (f"not classified: {vv} {s}" if e == "unknown" else EFFECT_WHY.get(e, e))
                     for b, e, s, vv in zip(ev["barcode"], effect, ev["series"], ev["voucher"])]
    return ev.drop(columns="event")


def explain_never(never, v, fetched):
    """For each sale of a label not seen in stock before it: what the ERP shows before the sale."""
    ev = never.reset_index(drop=True).rename_axis("event").reset_index()
    ev["since"] = ev["prev_snapshot"].fillna(ev["sale_date"] - pd.Timedelta(days=30))
    m = ev[["event", "barcode", "since", "sale_date"]].merge(v, on="barcode")
    m = m[m["doc_date"] <= m["sale_date"]]
    booked = set(m.loc[m["effect"] == "booking", "event"])
    ordered = set(m.loc[m["order_no"].notna(), "event"])
    labelled = m[m["effect"] == "labelling"].groupby("event")["doc_date"].max()
    first = m.groupby("event")["doc_date"].min()
    seen = set(fetched.loc[fetched["error"].isna(), "barcode"])
    why = []
    for e, b, since in zip(ev["event"], ev["barcode"], ev["since"]):
        if e in ordered:
            why.append("customer-order label (order labels are not in our stock pull)")
        elif e in booked:
            why.append("booked for a customer before the sale (booked pieces are not in the stock report)")
        elif e in labelled.index and labelled[e] >= since:
            why.append("labelled after the last snapshot before the sale")
        elif e in first.index:
            why.append(f"in the ERP since {first[e]:%d %b %Y}, missing from our snapshots")
        else:
            why.append("history not fetched" if b not in seen else "no ERP history before the sale")
    ev["erp_why"] = why
    return ev.drop(columns=["event", "since"])


def catalogue(a, conn, st, v, locations):
    """Every stock location and every ERP voucher type must be on the lists and classified (Settings)."""
    recent = st[st["snapshot_date"] >= st["snapshot_date"].max() - pd.Timedelta(days=365)]
    used = recent.groupby("location").size().rename("rows").reset_index()
    used = used.merge(locations, on="location", how="left")
    bad = used[used["kind"].isna() | (used["kind"] == "unclassified")]
    a.check(1, "Every stock location is on the location list and classified", "stock, last 365 days", 0, len(bad),
            note=", ".join(f"{r.location} ({r.rows:,} rows)" for r in bad.itertuples()) or "all classified")
    a.keep("L1 locations to classify", bad)
    if len(v):
        unk = v[v["effect"] == "unknown"].groupby(["voucher", "series"]).agg(vouchers=("doc_no", "size"),
                                                                           labels=("barcode", "nunique")).reset_index()
        a.check(1, "Every ERP voucher type on fetched label histories is classified", f"{v['barcode'].nunique():,} labels",
                0, len(unk), note=", ".join(f"{r.voucher} {r.series} ({r.vouchers:,})" for r in unk.itertuples()) or "all classified")
        a.keep("L1 voucher types to classify", unk)


def voucher_flow(a, left, arrived, never, v, fetched, bills, out_locations=()):
    """Layer 4: the ERP's own voucher history explains each movement our data can't."""
    gone = explain_left(left[left["why"] == "UNEXPLAINED"], v, fetched, bills, out_locations)
    came = explain_arrived(arrived[arrived["why"] == "UNEXPLAINED"], v, fetched)
    rest = explain_never(never[~never["between"]], v, fetched)
    for name, df in (("left", gone), ("arrived", came), ("sold, never in stock", rest)):
        counts = df["erp_why"].value_counts()
        mystery = int(df["erp_why"].isin(["no ERP voucher in the interval", "no ERP history before the sale",
                                          "history not fetched"]).sum()
                      + df["erp_why"].str.startswith("not classified").sum()
                      + df["erp_why"].str.contains("not in our sales|missing from our snapshot").sum())
        a.check(4, f"Every {name} label our data can't explain has an ERP voucher that does", f"{len(df):,} labels",
                0, mystery, note="; ".join(f"{k}: {n:,}" for k, n in counts.items()))
        a.keep(f"L4 {name}", df)


def run(today=None):
    today = today or date.today()
    a = Audit()
    with settings.etl_engine().connect() as conn:
        stock_loads(a, conn)
        sales_loads(a, conn)
        coverage(a, conn, today)
        st = stock_rows(conn)
        sa = sales_rows(conn)
        kinds = _read(conn, "SELECT erp_branch, kind FROM config.branch").set_index("erp_branch")["kind"]
        from kkj.scope import excluded_locations
        out_locations = excluded_locations(conn)
        v, fetched, locations, bills = voucher_data(conn)
    stock_fields(a, st)
    sales_fields(a, sa, today)
    same_dates(a, st, sa, v)
    catalogue(a, conn, st, v, locations)
    left, arrived, never = stock_flow(a, st, sa, kinds, out_locations)
    voucher_flow(a, left, arrived, never, v, fetched, bills, out_locations)
    return a


def write(a, out_dir=None):
    out_dir = out_dir or settings.PROJECT_DIR / "data" / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    res = pd.DataFrame(a.results)
    xlsx = out_dir / f"audit_{stamp}.xlsx"
    with pd.ExcelWriter(xlsx) as w:
        res.to_excel(w, sheet_name="Summary", index=False)
        for name, df in a.exceptions.items():
            df.head(100_000).to_excel(w, sheet_name=name[:31], index=False)
    counts = res["result"].value_counts().to_dict()
    lines = [f"# Data accuracy audit, {datetime.now():%d %b %Y %H:%M}", "",
             f"{counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, {counts.get('INFO', 0)} for information. "
             f"Exceptions in {xlsx.name}.", ""]
    for layer, g in res.groupby("layer"):
        lines += [f"## Layer {layer}", "", "| Result | Check | Scope | Expected | Actual | Note |", "|---|---|---|---|---|---|"]
        shown = pd.concat([g[g["result"] != "PASS"], g[g["result"] == "PASS"].drop_duplicates("check")])
        for r in shown.itertuples():
            scope = r.scope if r.result != "PASS" else ("all" if (g["check"] == r.check).sum() > 1 else r.scope)
            n = (g["check"] == r.check).sum()
            label = f"{r.result}" + (f" ×{n}" if r.result == "PASS" and n > 1 else "")
            lines.append(f"| {label} | {r.check} | {scope} | {r.expected} | {r.actual} | {r.note} |")
        lines.append("")
    md = out_dir / f"audit_{stamp}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return xlsx, md, counts


def save(a, xlsx, counts):
    """Record the run in brain.audit_run (shown on the Data quality page)."""
    import json
    exceptions = {k: int(len(v)) for k, v in a.exceptions.items()}
    results = pd.DataFrame(a.results).astype({"expected": str, "actual": str}).to_dict("records")
    with settings.app_engine().begin() as conn:
        conn.execute(text("""INSERT INTO brain.audit_run (passed, failed, info, results, exceptions, report_file)
                             VALUES (:p, :f, :i, CAST(:r AS jsonb), CAST(:e AS jsonb), :x)"""),
                     {"p": counts.get("PASS", 0), "f": counts.get("FAIL", 0), "i": counts.get("INFO", 0),
                      "r": json.dumps(results, default=str), "e": json.dumps(exceptions),
                      "x": str(xlsx.relative_to(settings.PROJECT_DIR))})


def audit_and_save():
    a = run()
    xlsx, md, counts = write(a)
    save(a, xlsx, counts)
    return xlsx, md, counts


def main():
    xlsx, md, counts = audit_and_save()
    print(f"{counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, {counts.get('INFO', 0)} info")
    print(f"Report: {md}\nExceptions: {xlsx}")
    sys.exit(0)


if __name__ == "__main__":
    main()
