"""
Dashboard data access: read-only (powerbi_reader), cached for 10 minutes.

All business rules live in kkj.metrics; this module only fetches rows.
"""
import pandas as pd
import streamlit as st
from sqlalchemy import text

from kkj import settings

TTL = 600


@st.cache_resource
def _engine():
    return settings.reader_engine()


def _read(sql, **params):
    with _engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


STOCK_COLUMNS = """snapshot_date, barcode, label_no, branch, location, item_class_group, item_name,
    category, supplier, pcs, gross_wt, net_wt, dia_wt, dia_amt, rate, item_amt, taxable_amount, age_days,
    days_from_branch_in, branch_in_date, coalesce(label_dt, labeling_date) AS label_date"""


@st.cache_data(ttl=TTL)
def stock_latest():
    """Every label in the latest production snapshot (erp.stock_snapshot)."""
    return _read(f"""
        SELECT {STOCK_COLUMNS} FROM erp.stock_snapshot
        WHERE snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot)""")


@st.cache_data(ttl=TTL)
def stock_on(snapshot_date):
    """Every label of one production snapshot day."""
    return _read(f"SELECT {STOCK_COLUMNS} FROM erp.stock_snapshot WHERE snapshot_date = :d", d=snapshot_date)


@st.cache_data(ttl=TTL)
def stock_series():
    """Company stock totals per date from every source; one row per date, best source wins.

    Preference: ERP daily (erp.stock_snapshot) > ERP as-on (stock_hist erp_asof) > legacy file.
    Value basis: taxable_amount, else item_amt (legacy files).
    """
    df = _read("""
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


@st.cache_data(ttl=TTL)
def sales_lines(date_from):
    """Sales lines from sales.v_sales_line (ERP where loaded, legacy before) since date_from."""
    df = _read("""
        SELECT source, sale_date, bill_no, barcode, label_no, item_name, category, item_class_group,
               branch, location, salesman, supplier, pcs, dia_wt, item_amt, taxable_amount,
               age_days_at_sale, inventory_ref_code, orig_bill_no, orig_sale_date, sales_return_done
        FROM sales.v_sales_line WHERE sale_date >= :d""", d=date_from)
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


@st.cache_data(ttl=TTL)
def stock_runs():
    return _read("""
        SELECT run_id, started_at AT TIME ZONE 'Asia/Kolkata' AS started_ist, source, status,
               record_count, rows_loaded, error, dq_warnings
        FROM erp.etl_run_log ORDER BY run_id DESC LIMIT 30""")


@st.cache_data(ttl=TTL)
def sales_loads():
    return _read("""
        SELECT load_id, started_at AT TIME ZONE 'Asia/Kolkata' AS started_ist, source, date_from, date_to,
               status, record_count, rows_loaded, error, dq_warnings
        FROM sales.load_log ORDER BY load_id DESC LIMIT 40""")


@st.cache_data(ttl=TTL)
def snapshot_status():
    return _read("""
        SELECT snapshot_date, source, status, reason, labels, file
        FROM stock_hist.snapshot_status ORDER BY snapshot_date DESC, source""")


@st.cache_data(ttl=TTL)
def stock_history(date_from):
    """Piece-level company stock on every snapshot date since date_from, best source per date.

    Sources by preference: ERP daily > ERP as-on > verified legacy file. value is on the
    common basis (taxable, else item_amt); age is recomputed as snapshot date - label date.
    """
    df = _read("""
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


@st.cache_data(ttl=60)
def rules_now():
    """Current business rules (config.setting over the defaults in kkj.rules)."""
    from kkj import rules
    with _engine().connect() as conn:
        return rules.load(conn)


@st.cache_data(ttl=60)
def branches():
    return _read("SELECT erp_branch, short_name, kind, active FROM config.branch")
