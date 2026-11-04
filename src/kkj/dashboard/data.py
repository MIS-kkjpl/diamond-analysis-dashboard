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
    category, supplier, pcs, gross_wt, dia_wt, item_amt, taxable_amount, age_days,
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
