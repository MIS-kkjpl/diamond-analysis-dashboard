"""
One-time database setup. Safe to re-run.

Uses the DIIS admin account (DIIS_DB_USER / DIIS_DB_PASSWORD) to:
  1. create database ERP_DB_NAME (kkj_erp_stock) if it doesn't exist
  2. create roles erp_etl, powerbi_reader and dashboard_app (LOGIN) if missing; if they exist,
     reset their passwords to the ones in .env so .env stays the source of truth
  3. run the sql/ files in settings.SQL_FILES inside the database (schemas, tables,
     indexes, views, grants); every file is safe to re-run

Run:  python -m kkj.db.setup                       # roles + every sql/ file
      python -m kkj.db.setup sales.sql stock_hist.sql   # only these files
Passwords are never printed. Ported from archive/inventory_project/db_setup.py,
which ran only sql/schema.sql.
"""
import sys

import psycopg
import sqlalchemy.exc
from psycopg import sql

from kkj import settings

# The sql/ files grant to these role names, so .env must use the same ones
SCHEMA_ROLES = ("erp_etl", "powerbi_reader", "dashboard_app")


def psycopg_conn(engine, autocommit):
    """The raw psycopg connection behind an SQLAlchemy engine."""
    raw = engine.raw_connection()
    conn = raw.driver_connection
    conn.autocommit = autocommit
    return raw, conn


def ensure_database_and_roles(db_name, roles):
    raw, conn = psycopg_conn(settings.admin_engine("postgres"), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone():
                print(f"Database {db_name}: already exists")
            else:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
                print(f"Database {db_name}: created")

            for user, password in roles:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
                exists = cur.fetchone() is not None
                verb = "ALTER ROLE" if exists else "CREATE ROLE"
                stmt = sql.SQL(verb + " {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(user), sql.Literal(password))
                try:
                    cur.execute(stmt)
                except psycopg.Error as e:
                    # Report only the server's message; the statement holds the password
                    raise SystemExit(f"ERROR: {verb} {user} failed: {e.diag.message_primary}")
                print(f"Role {user}: {'password synced with .env' if exists else 'created'}")

            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db_name),
                sql.SQL(", ").join(sql.Identifier(u) for u, _ in roles)))
    finally:
        raw.close()


def run_sql_file(db_name, name):
    path = settings.SQL_DIR / name
    raw, conn = psycopg_conn(settings.admin_engine(db_name), autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))   # no parameters, so several statements are allowed
        conn.commit()
        print(f"Ran sql/{name} in {db_name}")
    except psycopg.Error as e:
        conn.rollback()
        raise SystemExit(f"ERROR: sql/{name} failed: {e.diag.message_primary}")
    finally:
        raw.close()


def main(argv=None):
    files = list(argv if argv is not None else sys.argv[1:]) or settings.SQL_FILES
    unknown = [f for f in files if f not in settings.SQL_FILES]
    if unknown:
        sys.exit(f"ERROR: unknown sql file(s) {unknown}; choose from {settings.SQL_FILES}")
    try:
        db_name = settings.erp_db_name()
        roles = [(settings.require("ERP_DB_USER"), settings.require("ERP_DB_PASSWORD")),
                 (settings.require("PBI_DB_USER"), settings.require("PBI_DB_PASSWORD")),
                 (settings.require("APP_DB_USER"), settings.require("APP_DB_PASSWORD"))]
    except settings.ConfigError as e:
        sys.exit(f"ERROR: {e}")
    if tuple(u for u, _ in roles) != SCHEMA_ROLES:
        sys.exit(f"ERROR: ERP_DB_USER / PBI_DB_USER / APP_DB_USER must be {SCHEMA_ROLES} to match the sql/ files")

    try:
        ensure_database_and_roles(db_name, roles)
        for name in files:
            run_sql_file(db_name, name)
    except settings.ConfigError as e:
        sys.exit(f"ERROR: {e}")
    except (psycopg.OperationalError, sqlalchemy.exc.OperationalError) as e:
        # .orig is psycopg's own message (no password in it)
        sys.exit(f"ERROR: could not connect as the admin user: {getattr(e, 'orig', e)}")
    print("Setup complete.")


if __name__ == "__main__":
    main()
