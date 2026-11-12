"""
Database operations for the stock pipeline: run log + erp.stock_snapshot. No ERP or Excel logic.

kkj.pipelines.stock calls these in order:
  start_run -> update_run (as details become known) -> load_snapshot
  -> verify_snapshot -> finish_run

Ported unchanged from archive/inventory_project/db.py.
"""
import json

import numpy as np
import pandas as pd
from psycopg import sql
from sqlalchemy import text

from kkj import settings

_engine = None


def get_engine():
    """The erp_etl engine (created once per run)."""
    global _engine
    if _engine is None:
        _engine = settings.etl_engine()
    return _engine


STALE_AFTER = "1 hour"     # a 'running' row older than this was interrupted
STALE_ERROR = "interrupted (process killed or PC shut down)"


def close_stale_runs():
    """Mark runs left at 'running' for over STALE_AFTER as failed; return how many.

    A run that is killed (Task Scheduler time limit, PC shut down) never gets to
    record its result. Called at the start of every run.
    """
    with get_engine().begin() as conn:
        return conn.execute(
            text("""
                UPDATE erp.etl_run_log
                SET status = 'failed', error = :error, finished_at = now()
                WHERE status = 'running'
                  AND started_at < now() - CAST(:stale_after AS interval)
            """),
            {"error": STALE_ERROR, "stale_after": STALE_AFTER},
        ).rowcount


def start_run(source, report_id=None, raw_file=None, record_count=None):
    """Insert a run row with status 'running' and return its run_id.

    Called at the very start of a run; details not known yet are filled in
    later with update_run().
    """
    with get_engine().begin() as conn:
        return conn.execute(
            text("""
                INSERT INTO erp.etl_run_log (source, report_id, raw_file, record_count, status)
                VALUES (:source, :report_id, :raw_file, :record_count, 'running')
                RETURNING run_id
            """),
            {"source": source, "report_id": report_id, "raw_file": raw_file,
             "record_count": record_count},
        ).scalar_one()


# Run details that update_run() may fill in after start_run()
_RUN_DETAILS = ("report_id", "raw_file", "record_count")


def update_run(run_id, **details):
    """Fill in report_id / raw_file / record_count on a running run."""
    unknown = set(details) - set(_RUN_DETAILS)
    if unknown:
        raise ValueError(f"update_run: unknown fields {sorted(unknown)}")
    if not details:
        return
    # Column names come only from _RUN_DETAILS above; values are bound parameters
    assignments = ", ".join(f"{name} = :{name}" for name in details)
    with get_engine().begin() as conn:
        conn.execute(text(f"UPDATE erp.etl_run_log SET {assignments} WHERE run_id = :run_id"),
                     {"run_id": run_id, **details})


def _to_python(value):
    """pandas/numpy values -> plain Python values that psycopg's COPY understands."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    return value


def load_snapshot(df, snapshot_date, run_id, table="erp.stock_snapshot"):
    """Replace one day's rows in erp.stock_snapshot (or erp.order_snapshot): DELETE + COPY in ONE transaction.

    df must use the DB column names. Re-running a day replaces it, never duplicates.
    Rolls back on any error. Returns the number of rows loaded.
    """
    df = df.assign(run_id=run_id)
    columns = list(df.columns)
    schema, name = table.split(".")
    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(schema, name), sql.SQL(", ").join(sql.Identifier(c) for c in columns))

    raw = get_engine().raw_connection()
    conn = raw.driver_connection          # the underlying psycopg connection
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {} WHERE snapshot_date = %s").format(sql.Identifier(schema, name)),
                        (snapshot_date,))
            with cur.copy(copy_sql) as copy:
                for row in df.itertuples(index=False, name=None):
                    copy.write_row([_to_python(v) for v in row])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        raw.close()
    return len(df)


def finish_run(run_id, status, rows_loaded, validation, error=None, dq_warnings=None):
    """Close a run: status 'success' or 'failed', with all validation results
    and the data-quality warnings (None when cleaning never got that far)."""
    with get_engine().begin() as conn:
        conn.execute(
            text("""
                UPDATE erp.etl_run_log
                SET finished_at = now(), status = :status, rows_loaded = :rows_loaded,
                    validation = CAST(:validation AS jsonb), error = :error,
                    dq_warnings = CAST(:dq_warnings AS jsonb)
                WHERE run_id = :run_id
            """),
            {"run_id": run_id, "status": status, "rows_loaded": rows_loaded,
             "validation": json.dumps(validation, default=str), "error": error,
             "dq_warnings": None if dq_warnings is None else json.dumps(dq_warnings, default=str)},
        )


def verify_snapshot(snapshot_date, table="erp.stock_snapshot"):
    """Read back what is stored for one day: {count, sum_gross_wt, sum_item_amt}."""
    assert table in ("erp.stock_snapshot", "erp.order_snapshot")
    with get_engine().connect() as conn:
        row = conn.execute(
            text(f"""
                SELECT count(*), sum(gross_wt), sum(item_amt)
                FROM {table}
                WHERE snapshot_date = :d
            """),
            {"d": snapshot_date},
        ).one()
    count, gross, amt = row
    return {"count": count,
            "sum_gross_wt": float(gross) if gross is not None else None,
            "sum_item_amt": float(amt) if amt is not None else None}
