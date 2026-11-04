"""
Dashboard data access: cached wrappers around kkj.db.queries.

Reads go through powerbi_reader (read-only). Decisions and settings are written by the
pages themselves through dashboard_app. All business rules live in kkj.metrics / kkj.brain.
"""
import pandas as pd
import streamlit as st
from sqlalchemy import text

from kkj import settings
from kkj.db import queries as q

TTL = 600


@st.cache_resource
def _engine():
    return settings.reader_engine()


def _read(sql, **params):
    with _engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def _q(fn, *args):
    with _engine().connect() as conn:
        return fn(conn, *args)


@st.cache_data(ttl=TTL)
def stock_latest():
    return _q(q.stock_latest)


@st.cache_data(ttl=TTL)
def stock_on(snapshot_date):
    return _q(q.stock_on, snapshot_date)


@st.cache_data(ttl=TTL)
def stock_series():
    return _q(q.stock_series)


@st.cache_data(ttl=TTL)
def stock_history(date_from):
    return _q(q.stock_history, date_from)


@st.cache_data(ttl=TTL)
def sales_lines(date_from):
    return _q(q.sales_lines, date_from)


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


@st.cache_data(ttl=60)
def rules_now():
    """Current business rules (config.setting over the defaults in kkj.rules)."""
    from kkj import rules
    with _engine().connect() as conn:
        return rules.load(conn)


@st.cache_data(ttl=60)
def branches():
    return _q(q.branches)
