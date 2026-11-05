"""
Read queries shared by the web app (cached in kkj.web.common) and the recommendation
engine (kkj.brain.run). Each takes an open SQLAlchemy connection and returns a DataFrame.
"""
import pandas as pd
from sqlalchemy import text

STOCK_COLUMNS = """snapshot_date, barcode, label_no, branch, location, item_class_group, item_name,
    category, supplier, pcs, gross_wt, net_wt, dia_wt, dia_amt, rate, item_amt, taxable_amount, age_days,
    days_from_branch_in, branch_in_date, coalesce(label_dt, labeling_date) AS label_date"""


def read(conn, sql, **params):
    return pd.read_sql(text(sql), conn, params=params)


def stock_latest(conn):
    """Every label in the latest production snapshot (erp.stock_snapshot)."""
    return read(conn, f"""SELECT {STOCK_COLUMNS} FROM erp.stock_snapshot
                         WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot)""")


def stock_on(conn, snapshot_date):
    return read(conn, f"SELECT {STOCK_COLUMNS} FROM erp.stock_snapshot WHERE snapshot_date = :d", d=snapshot_date)


def stock_series(conn):
    """Company stock totals per date, best source per date (ERP daily > ERP as-on > legacy file)."""
    df = read(conn, """
        SELECT snapshot_date, 'ERP daily' AS source, 1 AS rank,
               sum(coalesce(taxable_amount, item_amt)) AS value, sum(pcs) AS pcs, count(*) AS labels
        FROM erp.stock_snapshot WHERE item_class_group <> 'Customer Diamond' GROUP BY 1
        UNION ALL
        SELECT snapshot_date, CASE source WHEN 'erp_asof' THEN 'ERP as-on' ELSE 'Legacy file' END,
               CASE source WHEN 'erp_asof' THEN 2 ELSE 3 END,
               sum(coalesce(taxable_amount, item_amt)), sum(pcs), count(*)
        FROM stock_hist.snapshot WHERE item_class_group <> 'Customer Diamond' GROUP BY 1, 2, 3""")
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df = df.sort_values(["snapshot_date", "rank"]).drop_duplicates("snapshot_date")
    return df.drop(columns="rank").reset_index(drop=True)


def stock_history(conn, date_from):
    """Piece-level company stock on every snapshot date since date_from, best source per date.

    value is on the common basis (taxable, else item_amt); age = snapshot date - label date.
    """
    df = read(conn, """
        WITH all_rows AS (
            SELECT snapshot_date, 1 AS rank, barcode, branch, location, item_class_group, item_name, supplier, pcs,
                   coalesce(taxable_amount, item_amt) AS value, coalesce(label_dt, labeling_date) AS label_date
            FROM erp.stock_snapshot WHERE snapshot_date >= :d
            UNION ALL
            SELECT snapshot_date, CASE source WHEN 'erp_asof' THEN 2 ELSE 3 END, barcode, branch, location,
                   item_class_group, item_name, supplier, pcs, coalesce(taxable_amount, item_amt),
                   coalesce(label_dt, labeling_date)
            FROM stock_hist.snapshot WHERE snapshot_date >= :d),
        best AS (SELECT snapshot_date, min(rank) AS rank FROM all_rows GROUP BY 1)
        SELECT a.* FROM all_rows a JOIN best USING (snapshot_date, rank)
        WHERE a.item_class_group <> 'Customer Diamond'""", d=date_from)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df["age"] = (df["snapshot_date"] - pd.to_datetime(df["label_date"])).dt.days
    df["source"] = df.pop("rank").map({1: "ERP daily", 2: "ERP as-on", 3: "Legacy file"})
    return df


def stock_dates(conn):
    """Every date with usable stock and its best source: rank 1 ERP daily, 2 ERP as-on, 3 legacy file."""
    df = read(conn, """
        SELECT snapshot_date, min(rank) AS rank FROM (
            SELECT DISTINCT snapshot_date, 1 AS rank FROM erp.stock_snapshot
            UNION ALL
            SELECT snapshot_date, CASE source WHEN 'erp_asof' THEN 2 ELSE 3 END
            FROM stock_hist.snapshot_status WHERE status = 'loaded') d
        GROUP BY 1 ORDER BY 1""")
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df


BRICK_STOCK = """snapshot_date, branch, location, item_class_group, purity, item_name, category, sub_category,
    dia_wt, gross_wt, pcs, coalesce(taxable_amount, item_amt) AS value"""


def brick_stock(conn, dates):
    """Company stock rows (brick columns) on the given snapshot dates, each date from its best source."""
    best = stock_dates(conn)
    best = best[best["snapshot_date"].isin(pd.to_datetime(pd.Series(list(dates))))]
    erp = [d.date() for d in best.loc[best["rank"] == 1, "snapshot_date"]]
    asof = [d.date() for d in best.loc[best["rank"] == 2, "snapshot_date"]]
    legacy = [d.date() for d in best.loc[best["rank"] == 3, "snapshot_date"]]
    df = read(conn, f"""
        SELECT {BRICK_STOCK} FROM erp.stock_snapshot WHERE snapshot_date = ANY(:erp)
        UNION ALL
        SELECT {BRICK_STOCK} FROM stock_hist.snapshot
        WHERE (source = 'erp_asof' AND snapshot_date = ANY(:asof)) OR (source = 'csv_legacy' AND snapshot_date = ANY(:legacy))""",
              erp=erp, asof=asof, legacy=legacy)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df[df["item_class_group"] != "Customer Diamond"]


def brick_sales(conn, date_from):
    """Net sale lines (returns negative) with the brick columns, since date_from.

    A sold piece belongs to the brick it was stocked in, so its brick columns come from its own
    label in stock history (matched by barcode; a return by the sale it reverses) and from the sale
    line only when there is no match. Legacy sale lines often lack the diamond weight.
    """
    df = read(conn, """
        WITH label AS (
            SELECT DISTINCT ON (barcode) barcode, item_class_group, purity, item_name, category, sub_category,
                   dia_wt, gross_wt
            FROM (SELECT snapshot_date, barcode, item_class_group, purity, item_name, category, sub_category, dia_wt,
                         gross_wt FROM erp.stock_snapshot
                  UNION ALL
                  SELECT snapshot_date, barcode, item_class_group, purity, item_name, category, sub_category, dia_wt,
                         gross_wt FROM stock_hist.snapshot) s
            WHERE barcode IS NOT NULL
            ORDER BY barcode, snapshot_date DESC),
        line AS (
            SELECT v.*, coalesce(v.barcode, o.barcode) AS label_barcode
            FROM sales.v_sales_line v
            LEFT JOIN sales.v_sales_line o ON v.pcs < 0 AND o.bill_no = v.orig_bill_no
                 AND o.inventory_ref_code = v.inventory_ref_code AND o.pcs > 0
            WHERE v.sale_date >= :d AND v.item_class_group <> 'Customer Diamond')
        SELECT l.sale_date, l.branch, coalesce(s.item_class_group, l.item_class_group) AS item_class_group,
               coalesce(s.purity, l.purity) AS purity, coalesce(s.item_name, l.item_name) AS item_name,
               CASE WHEN s.barcode IS NOT NULL THEN s.category ELSE l.category END AS category,
               CASE WHEN s.barcode IS NOT NULL THEN s.sub_category ELSE l.sub_category END AS sub_category,
               CASE WHEN s.barcode IS NOT NULL THEN s.dia_wt ELSE l.dia_wt END AS dia_wt,
               CASE WHEN s.barcode IS NOT NULL THEN s.gross_wt ELSE l.gross_wt END AS gross_wt,
               l.pcs, coalesce(l.taxable_amount, l.item_amt) AS value, s.barcode IS NOT NULL AS from_label
        FROM line l LEFT JOIN label s ON s.barcode = l.label_barcode""", d=date_from)
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


def sales_lines(conn, date_from):
    """Sales lines from sales.v_sales_line (ERP where loaded, legacy before) since date_from."""
    df = read(conn, """
        SELECT source, sale_date, bill_no, barcode, label_no, item_name, category, item_class_group,
               branch, location, salesman, supplier, pcs, dia_wt, item_amt, taxable_amount,
               age_days_at_sale, inventory_ref_code, orig_bill_no, orig_sale_date, sales_return_done
        FROM sales.v_sales_line WHERE sale_date >= :d""", d=date_from)
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


def hist_daily(conn):
    """Pieces sold per day, item group and product over the five-year history (sales.hist_line)."""
    df = read(conn, """SELECT sale_date, item_class_group, item_name, sum(pcs) AS pcs, sum(item_amt) AS value
                       FROM sales.hist_line GROUP BY 1, 2, 3""")
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    # the exports spell some names differently by year ('GOLD' / 'Gold'): one spelling per name, the commonest
    for col in ("item_class_group", "item_name"):
        key = df[col].fillna("").str.strip().str.casefold()
        commonest = df.assign(k=key).groupby(["k", col])["pcs"].sum().reset_index() \
            .sort_values("pcs", ascending=False).drop_duplicates("k").set_index("k")[col]
        df[col] = key.map(commonest)
    return df.groupby(["sale_date", "item_class_group", "item_name"], as_index=False)[["pcs", "value"]].sum()


def festivals(conn):
    df = read(conn, "SELECT name, day, note FROM config.festival ORDER BY day")
    df["day"] = pd.to_datetime(df["day"])
    return df


def ideal_turns(conn):
    """config.ideal_turn: (level, key, ideal) set in Settings -> Ideal turns."""
    return read(conn, "SELECT level, key, ideal::float AS ideal, note, updated_at, updated_by FROM config.ideal_turn")


def branches(conn):
    return read(conn, "SELECT erp_branch, short_name, kind, active FROM config.branch")


def salespeople(conn):
    return read(conn, "SELECT sales_code, name, branch, kind, active FROM config.salesperson")
