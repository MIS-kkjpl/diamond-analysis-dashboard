"""
A data notice shown on every page of the app (e.g. "sales exclude order-booked pieces until the next
refresh"). Kept in config.setting under the key data_notice; the job that fixes the data clears it.
"""
import json

from sqlalchemy import text

KEY = "data_notice"


def get(conn):
    value = conn.execute(text("SELECT value FROM config.setting WHERE key = :k"), {"k": KEY}).scalar()
    return value if value else None


def set_notice(conn, message, by):
    conn.execute(text("""INSERT INTO config.setting (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :by)
                         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                         updated_by = EXCLUDED.updated_by"""), {"k": KEY, "v": json.dumps({"text": message}), "by": by})


def clear(conn, by):
    conn.execute(text("UPDATE config.setting SET value = 'null'::jsonb, updated_at = now(), updated_by = :by "
                      "WHERE key = :k"), {"k": KEY, "by": by})
