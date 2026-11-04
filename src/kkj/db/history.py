"""
Database operations for the history schemas: sales.* and stock_hist.*. No file or ERP logic.

Every load replaces its slice (a source's date range, or one snapshot date) in ONE
transaction: DELETE + COPY, so re-running never duplicates and a failure leaves
the previous rows intact.
"""
import json

import pandas as pd
from psycopg import sql
from sqlalchemy import text

from kkj.db.stock import _to_python, get_engine


def _copy(cur, table, df):
    """COPY a DataFrame (DB column names) into schema.table on an open psycopg cursor."""
    schema, name = table.split(".")
    stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(schema), sql.Identifier(name),
        sql.SQL(", ").join(sql.Identifier(c) for c in df.columns))
    with cur.copy(stmt) as copy:
        for row in df.itertuples(index=False, name=None):
            copy.write_row([_to_python(v) for v in row])


def _transaction(work):
    """Run work(cursor) in one psycopg transaction; roll back on any error."""
    raw = get_engine().raw_connection()
    conn = raw.driver_connection
    try:
        with conn.cursor() as cur:
            result = work(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        raw.close()


def location_branch_map():
    """{location: branch} from the production stock, for locations that map to one branch only."""
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT location, min(branch)
            FROM erp.stock_snapshot
            WHERE location IS NOT NULL AND branch IS NOT NULL
            GROUP BY location
            HAVING count(DISTINCT branch) = 1
        """)).all()
    return dict(rows)


def stock_barcodes():
    """Distinct (item_name, barcode, label_no) from the production stock: the barcode naming rules."""
    with get_engine().connect() as conn:
        return pd.read_sql(text("""
            SELECT DISTINCT item_name, barcode, label_no FROM erp.stock_snapshot
            WHERE item_name IS NOT NULL AND label_no IS NOT NULL
        """), conn)


# ── sales ────────────────────────────────────────────────────────────────────
def start_sales_load(source, date_from, date_to, raw_file=None, report_id=None, record_count=None):
    with get_engine().begin() as conn:
        return conn.execute(text("""
            INSERT INTO sales.load_log (source, date_from, date_to, raw_file, report_id, record_count)
            VALUES (:source, :date_from, :date_to, :raw_file, :report_id, :record_count)
            RETURNING load_id
        """), {"source": source, "date_from": date_from, "date_to": date_to, "raw_file": raw_file,
               "report_id": report_id, "record_count": record_count}).scalar_one()


def finish_sales_load(load_id, status, rows_loaded, validation, error=None, dq_warnings=None):
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE sales.load_log
            SET finished_at = now(), status = :status, rows_loaded = :rows_loaded,
                validation = CAST(:validation AS jsonb), error = :error,
                dq_warnings = CAST(:dq AS jsonb)
            WHERE load_id = :load_id
        """), {"load_id": load_id, "status": status, "rows_loaded": rows_loaded,
               "validation": json.dumps(validation, default=str), "error": error,
               "dq": None if dq_warnings is None else json.dumps(dq_warnings, default=str)})


def replace_sales(df, source, date_from, date_to, load_id):
    """Replace one source's rows for the ERP's report scope [date_from, date_to]; return rows loaded.

    The scope is the ORIGINAL sale date (orig_sale_date; legacy rows: sale_date), so a
    return dated after the range is replaced by the pull it came in, and never deleted by
    the pull of the month it is dated in. Rows with the same (bill_no, inventory_ref_code)
    are replaced too, wherever they were loaded from.
    """
    df = df.assign(source=source, load_id=load_id)
    if "inventory_ref_code" in df:
        keys = df.loc[df["inventory_ref_code"].notna(), ["bill_no", "inventory_ref_code"]]
    else:
        keys = df.iloc[0:0]

    def work(cur):
        cur.execute("""
            DELETE FROM sales.sales_line
            WHERE source = %s AND coalesce(orig_sale_date, sale_date) BETWEEN %s AND %s
        """, (source, date_from, date_to))
        if len(keys):
            cur.execute("""
                DELETE FROM sales.sales_line s
                USING unnest(%s::text[], %s::bigint[]) AS k(bill_no, ref)
                WHERE s.source = %s AND s.bill_no = k.bill_no AND s.inventory_ref_code = k.ref
            """, (keys["bill_no"].tolist(), [int(v) for v in keys["inventory_ref_code"]], source))
        _copy(cur, "sales.sales_line", df)
        return len(df)
    return _transaction(work)


def load_totals(load_id):
    """Read back {count, pcs, item_amt} of the rows one load wrote."""
    with get_engine().connect() as conn:
        n, pcs, amt = conn.execute(text("""
            SELECT count(*), sum(pcs), sum(item_amt) FROM sales.sales_line WHERE load_id = :l
        """), {"l": load_id}).one()
    return {"count": n, "pcs": float(pcs or 0), "item_amt": float(amt or 0)}


def sales_totals(source, date_from, date_to):
    """Read back {count, pcs, item_amt} for one source and date range."""
    with get_engine().connect() as conn:
        n, pcs, amt = conn.execute(text("""
            SELECT count(*), sum(pcs), sum(item_amt) FROM sales.sales_line
            WHERE source = :s AND sale_date BETWEEN :a AND :b
        """), {"s": source, "a": date_from, "b": date_to}).one()
    return {"count": n, "pcs": float(pcs or 0), "item_amt": float(amt or 0)}


def sold_barcodes(date_from, date_to):
    """(barcode, sale_date) sold in the range, from sales.v_sales_line (ERP where loaded,
    legacy before that). Lines whose barcode is still the bill no and returns are left out."""
    with get_engine().connect() as conn:
        return pd.read_sql(text("""
            SELECT barcode, sale_date FROM sales.v_sales_line
            WHERE sale_date BETWEEN :a AND :b AND pcs > 0
              AND barcode IS NOT NULL AND barcode IS DISTINCT FROM bill_no
        """), conn, params={"a": date_from, "b": date_to})


# ── stock_hist ───────────────────────────────────────────────────────────────
def replace_stock_hist(df, snapshot_date, source, status):
    """Replace one snapshot (rows + status row). df=None records a quarantined file, no rows.

    status: {file, status, reason, labels, checks}. Returns rows loaded.
    """
    def work(cur):
        cur.execute("DELETE FROM stock_hist.snapshot WHERE snapshot_date = %s AND source = %s",
                    (snapshot_date, source))
        cur.execute("DELETE FROM stock_hist.snapshot_status WHERE snapshot_date = %s AND source = %s",
                    (snapshot_date, source))
        cur.execute("""
            INSERT INTO stock_hist.snapshot_status (snapshot_date, source, file, status, reason, labels, checks)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """, (snapshot_date, source, status["file"], status["status"], status["reason"],
              status["labels"], json.dumps(status["checks"], default=str)))
        if df is None:
            return 0
        _copy(cur, "stock_hist.snapshot", df.assign(snapshot_date=snapshot_date, source=source))
        return len(df)
    return _transaction(work)


def stock_hist_totals(snapshot_date, source):
    with get_engine().connect() as conn:
        n, pcs, amt = conn.execute(text("""
            SELECT count(*), sum(pcs), sum(item_amt) FROM stock_hist.snapshot
            WHERE snapshot_date = :d AND source = :s
        """), {"d": snapshot_date, "s": source}).one()
    return {"count": n, "pcs": float(pcs or 0), "item_amt": float(amt or 0)}
