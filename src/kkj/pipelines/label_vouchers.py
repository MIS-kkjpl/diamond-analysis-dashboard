"""
Each label's ERP voucher history (the Label Status screen) into erp.label_voucher, so the audit and the app know
what actually happened to a piece: sale, customer booking, Stock Outward, repair, transfer, labelling ...

Which labels:
  --unexplained   every label our own data can't explain (left without a retail sale, arrived without being new
                  or returned, sold without being held in stock), June 2025 onwards; skips labels already fetched
                  after the event
  --recent N      every label that left or arrived in the last N snapshot intervals, explained or not (the daily
                  refresh uses --recent 1)
  --barcodes ...  these labels
  --all-labels    every diamond label we ever held or sold, in every financial year it existed (from 2021-22),
                  newest years first; resumable (skips what is fetched), --until HH:MM stops it in time. Gives each
                  label's days on display per location, for measuring demand per day on display.

A label's history lives in the financial-year company of the event (current year: the normal login; earlier
years: ERP_<year>_* in .env), so each label is fetched from the year(s) of its events. One small call per label.

Run:  python -m kkj.pipelines.label_vouchers --unexplained
"""
import argparse
import logging
import re
import time
from datetime import date

import pandas as pd
from sqlalchemy import text

from kkj import settings
from kkj.erp import label_status
from kkj.erp.client import login, new_session

log = logging.getLogger("label_vouchers")
FIRST_FY_START = date(2025, 4, 1)          # the oldest company the audit needs (stock flow from June 2025)


def fy_of(day):
    """'2026-27' for a date in Apr 2026 - Mar 2027."""
    day = pd.Timestamp(day)
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def current_fy():
    return fy_of(date.today())


def series_of(doc_no):
    """MHODDJ-288 -> MHODDJ; MK2SOS-1873 -> MK2SOS."""
    m = re.match(r"^(.*?)[-/ ]?\d+$", str(doc_no or "").strip())
    return m.group(1).rstrip("-/ ") if m else str(doc_no or "")


def targets(conn, unexplained=False, recent=0):
    """(barcode, fy) pairs to fetch, with the date of the event that needs explaining."""
    from kkj.checks import audit
    from kkj.scope import excluded_locations
    st, sa = audit.stock_rows(conn), audit.sales_rows(conn)
    left, arrived, never, chain = audit.flow_events(st, sa, excluded_locations(conn))
    ev = []
    if unexplained:
        ev += [left.loc[left["why"] == "UNEXPLAINED", ["barcode", "last_seen", "missing_from"]].set_axis(["barcode", "d0", "d1"], axis=1),
               arrived.loc[arrived["why"] == "UNEXPLAINED", ["barcode", "not_in", "first_seen"]].set_axis(["barcode", "d0", "d1"], axis=1),
               never.loc[~never["between"], ["barcode", "prev_snapshot", "sale_date"]].set_axis(["barcode", "d0", "d1"], axis=1)]
    if recent:
        dates = sorted(chain["snapshot_date"].unique())[-(recent + 1):]
        ev += [left.loc[left["missing_from"].isin(dates[1:]), ["barcode", "last_seen", "missing_from"]].set_axis(["barcode", "d0", "d1"], axis=1),
               arrived.loc[arrived["first_seen"].isin(dates[1:]), ["barcode", "not_in", "first_seen"]].set_axis(["barcode", "d0", "d1"], axis=1)]
    if not ev:
        return pd.DataFrame(columns=["barcode", "fy", "d1"])
    ev = pd.concat(ev).dropna(subset=["barcode"])
    ev["d0"], ev["d1"] = pd.to_datetime(ev["d0"]).fillna(pd.to_datetime(ev["d1"])), pd.to_datetime(ev["d1"])
    pairs = pd.concat([ev.assign(fy=ev["d0"].map(fy_of)), ev.assign(fy=ev["d1"].map(fy_of))])
    pairs = pairs[pairs["fy"] >= fy_of(FIRST_FY_START)]
    return pairs.groupby(["barcode", "fy"], as_index=False)["d1"].max()


HISTORY_FROM = date(2021, 4, 1)


def all_labels(conn):
    """(barcode, fy) for every diamond label held or sold since HISTORY_FROM, each financial year from its first to
    its last sighting; d1 = the end of that year (or today), so a closed year fetched once is never fetched again."""
    from kkj.scope import DIAMOND_GROUPS
    b = pd.read_sql(text("""
        WITH s AS (SELECT barcode, snapshot_date d FROM stock_hist.snapshot WHERE item_class_group = ANY(:g)
                   UNION ALL SELECT barcode, snapshot_date FROM erp.stock_snapshot WHERE item_class_group = ANY(:g)
                   UNION ALL SELECT barcode, sale_date FROM sales.v_sales_line
                             WHERE source = 'erp' AND item_class_group = ANY(:g))
        SELECT barcode, min(d) AS f, max(d) AS l FROM s WHERE barcode IS NOT NULL AND barcode <> '' GROUP BY 1"""),
                    conn, params={"g": DIAMOND_GROUPS})
    rows = []
    for bc, f, l in zip(b["barcode"], pd.to_datetime(b["f"]), pd.to_datetime(b["l"])):
        y0 = max(f, pd.Timestamp(HISTORY_FROM))
        y0 = y0.year if y0.month >= 4 else y0.year - 1
        y1 = l.year if l.month >= 4 else l.year - 1
        for y in range(y0, y1 + 1):
            rows.append((bc, f"{y}-{str(y + 1)[2:]}", min(pd.Timestamp(f"{y + 1}-03-31"), pd.Timestamp.today().normalize())))
    out = pd.DataFrame(rows, columns=["barcode", "fy", "d1"])
    # the 2021-22 and 2022-23 logins open the same ERP company (vouchers 31/03/2021 - 31/03/2023): fetch it once
    out["fy"] = out["fy"].replace(SAME_COMPANY)
    return out.groupby(["barcode", "fy"], as_index=False)["d1"].max()


SAME_COMPANY = {"2021-22": "2022-23"}


def due(conn, pairs):
    """Drop pairs already fetched after their event (a fetch before the event can't show it)."""
    done = pd.read_sql(text("SELECT barcode, fy, fetched_at FROM erp.label_voucher_fetch WHERE error IS NULL"), conn)
    if done.empty or pairs.empty:
        return pairs
    m = pairs.merge(done, on=["barcode", "fy"], how="left")
    fetched = pd.to_datetime(m["fetched_at"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return m[fetched.isna() | (fetched.dt.normalize() <= m["d1"])][["barcode", "fy", "d1"]]


def rows_of(barcode, fy, data):
    h = label_status.history(data)
    if h.empty:
        return []
    return [{"barcode": barcode, "fy": fy, "seq": int(r.seq), "doc_date": r.docDate.date() if pd.notna(r.docDate) else None,
             "doc_no": r.docNo, "series": series_of(r.docNo), "voucher": r.voucherMaster, "tx_type": r.transactionType,
             "pieces": r.pieces, "pieces_out": r.piecesOut, "location": r.locationName or None,
             "order_no": None if pd.isna(r.orderNo) else str(r.orderNo), "tag": None if pd.isna(r.tag) else str(r.tag),
             "cancelled": str(r.cancelFlag).strip().lower() in ("y", "yes", "true", "1")}
            for r in h.itertuples()]


def save(conn, barcode, fy, rows, error=None):
    conn.execute(text("DELETE FROM erp.label_voucher WHERE barcode = :b AND fy = :f"), {"b": barcode, "f": fy})
    if rows:
        conn.execute(text("""INSERT INTO erp.label_voucher (barcode, fy, seq, doc_date, doc_no, series, voucher, tx_type,
                                 pieces, pieces_out, location, order_no, tag, cancelled)
                             VALUES (:barcode, :fy, :seq, :doc_date, :doc_no, :series, :voucher, :tx_type, :pieces,
                                     :pieces_out, :location, :order_no, :tag, :cancelled)"""), rows)
    conn.execute(text("""INSERT INTO erp.label_voucher_fetch (barcode, fy, fetched_at, vouchers, error)
                         VALUES (:b, :f, now(), :n, :e)
                         ON CONFLICT (barcode, fy) DO UPDATE SET fetched_at = now(), vouchers = EXCLUDED.vouchers,
                             error = EXCLUDED.error"""), {"b": barcode, "f": fy, "n": len(rows), "e": error})


def fetch(pairs, pause=0.15, until=None):
    """Fetch every (barcode, fy), newest year first; one login per financial year. Stops at `until` (a datetime).
    Returns (fetched, failed)."""
    engine = settings.etl_engine()
    ok = bad = 0
    for fy, group in sorted(pairs.groupby("fy"), key=lambda x: x[0], reverse=True):
        if until and pd.Timestamp.now() >= until:
            break
        cfg = settings.erp_login_settings(None if fy == current_fy() else fy)
        s = new_session(cfg)
        login(s, cfg)
        log.info(f"{fy}: {len(group)} labels")
        for i, b in enumerate(group["barcode"], 1):
            try:
                try:
                    data = label_status.fetch(s, b, cfg["role_code"])
                except Exception:                      # one retry after a fresh login (session may have expired)
                    login(s, cfg)
                    data = label_status.fetch(s, b, cfg["role_code"])
                with engine.begin() as conn:
                    save(conn, b, fy, rows_of(b, fy, data))
                ok += 1
            except Exception as e:                     # noqa: BLE001 (recorded, retried on the next run)
                with engine.begin() as conn:
                    save(conn, b, fy, [], error=f"{type(e).__name__}: {e}"[:300])
                bad += 1
            if i % 500 == 0:
                log.info(f"  {i}/{len(group)}")
            time.sleep(pause)
            if until and pd.Timestamp.now() >= until:
                log.info(f"Stopped at {until:%H:%M} as asked; run again to continue")
                break
    return ok, bad


LOCATION_GUESS = """CASE WHEN location ILIKE '%approval%' THEN 'approval'
                         WHEN location ~* 'trans' THEN 'in_transit'
                         WHEN location ILIKE '%repair%' THEN 'repair'
                         WHEN location ~* 'z counter|supply' THEN 'unclassified'
                         WHEN location ~* 'counter|branch|mo diamond|exhibition' THEN 'counter'
                         ELSE 'unclassified' END"""


def sync_catalogue(conn):
    """Put every location seen in stock or in a voucher, and every voucher type / series seen, on the lists
    (config.location, config.voucher_type) so nothing goes unnoticed: a new location gets a kind guessed from its
    name ('unclassified' when unclear), a new voucher type or Stock Outward series gets effect 'unknown'. The audit
    fails on anything unclassified until someone sets it in Settings. Returns (new locations, new voucher types)."""
    locs = conn.execute(text(f"""
        INSERT INTO config.location (location, kind, updated_by)
        SELECT location, {LOCATION_GUESS}, 'auto' FROM (
            SELECT DISTINCT location FROM erp.stock_snapshot UNION SELECT DISTINCT location FROM stock_hist.snapshot
            UNION SELECT DISTINCT location FROM erp.label_voucher) s
        WHERE location IS NOT NULL AND location <> ''
        ON CONFLICT (location) DO NOTHING RETURNING location""")).scalars().all()
    vts = conn.execute(text("""
        INSERT INTO config.voucher_type (voucher, series, effect, updated_by)
        SELECT DISTINCT v.voucher, CASE WHEN EXISTS (SELECT 1 FROM config.voucher_type t WHERE t.voucher = v.voucher)
                                        THEN v.series ELSE '*' END, 'unknown', 'auto'
        FROM erp.label_voucher v
        WHERE NOT EXISTS (SELECT 1 FROM config.voucher_type t WHERE t.voucher = v.voucher AND t.series IN (v.series, '*'))
        ON CONFLICT (voucher, series) DO NOTHING RETURNING voucher || ' ' || series""")).scalars().all()
    return locs, vts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch ERP voucher histories of labels into erp.label_voucher")
    ap.add_argument("--unexplained", action="store_true")
    ap.add_argument("--recent", type=int, default=0)
    ap.add_argument("--barcodes", nargs="*")
    ap.add_argument("--fy", help="with --barcodes: financial year (default current)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true", help="refetch even if already fetched after the event")
    ap.add_argument("--all-labels", action="store_true", help="every diamond label, every year it existed (resumable)")
    ap.add_argument("--until", help="stop at this time today, HH:MM")
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between calls")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = settings.etl_engine()
    if args.barcodes:
        pairs = pd.DataFrame({"barcode": args.barcodes, "fy": args.fy or current_fy(), "d1": pd.Timestamp.today()})
    else:
        with engine.connect() as conn:
            pairs = all_labels(conn) if args.all_labels else targets(conn, args.unexplained, args.recent)
            if not args.all:
                pairs = due(conn, pairs)
    if args.limit:
        pairs = pairs.head(args.limit)
    log.info(f"{len(pairs)} label histories to fetch")
    until = pd.Timestamp(f"{date.today()} {args.until}") if args.until else None
    ok, bad = fetch(pairs, pause=args.pause, until=until)
    log.info(f"Fetched {ok}, failed {bad}")
    with engine.begin() as conn:
        locs, vts = sync_catalogue(conn)
    if locs or vts:
        log.info(f"New on the lists (classify in Settings): locations {locs}; voucher types {vts}")
    raise SystemExit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
