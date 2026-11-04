"""
Project settings: reads .env, defines paths and builds SQLAlchemy engines. Nothing else.

Required settings raise ConfigError naming the missing variable. Passwords have
no defaults. Engines are built with URL.create(), so passwords containing "@"
or other special characters are safe; never build a connection URL with an f-string.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

PROJECT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_DIR / ".env")

# ── Paths ────────────────────────────────────────────────────────────────────
CONFIG_DIR = PROJECT_DIR / "config"
STOCK_PAYLOAD_FILE = CONFIG_DIR / "stock_payload.json"   # stock report filters (from the browser)
SQL_DIR = PROJECT_DIR / "sql"
# Run by kkj.db.setup, in this order: schema erp (production stock), then the newer schemas
SQL_FILES = ["schema.sql", "sales.sql", "stock_hist.sql", "config.sql", "brain.sql", "auth.sql"]
DATA_DIR = PROJECT_DIR / "data"
STOCK_RAW_DIR = DATA_DIR / "stock" / "raw"        # original server .xlsx files, never modified
STOCK_META_DIR = DATA_DIR / "stock" / "meta"      # step A JSON responses
STOCK_CLEAN_DIR = DATA_DIR / "stock" / "clean"    # optional clean .parquet copies
LOGS_DIR = PROJECT_DIR / "logs"                   # run_<YYYYMMDD_HHMM>.log


class ConfigError(Exception):
    """A required setting is missing from .env."""


def require(name):
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required setting {name} in .env (see .env.example)")
    return value


def optional(name, default):
    return os.getenv(name) or default


# ── ERP login ────────────────────────────────────────────────────────────────
def erp_login_settings():
    return {
        "username": require("ERP_USERNAME"),
        "password": require("ERP_PASSWORD"),
        "x_key": require("ERP_API_KEY"),
        "role_code": optional("ERP_ROLE_CODE", "2665"),
    }


# ── Database engines ─────────────────────────────────────────────────────────
def erp_db_name():
    return require("ERP_DB_NAME")


def _engine(user_var, password_var, database, **kwargs):
    url = URL.create(
        "postgresql+psycopg",
        username=require(user_var),
        password=require(password_var),
        host=optional("DIIS_DB_HOST", "localhost"),
        port=int(optional("DIIS_DB_PORT", "5432")),
        database=database,
    )
    # hide_parameters keeps bound values out of error messages and logs
    return create_engine(url, hide_parameters=True, pool_pre_ping=True, **kwargs)


def admin_engine(database="postgres"):
    """DIIS admin account: used ONLY by kkj.db.setup."""
    return _engine("DIIS_DB_USER", "DIIS_DB_PASSWORD", database)


def etl_engine():
    """erp_etl: the pipeline's account (full rights on schema erp)."""
    return _engine("ERP_DB_USER", "ERP_DB_PASSWORD", erp_db_name())


def app_engine():
    """dashboard_app: reads everything, writes only schema config (settings, salesperson list)."""
    return _engine("APP_DB_USER", "APP_DB_PASSWORD", erp_db_name())


def reader_engine():
    """powerbi_reader: read-only on schema erp (the dashboard reads with this)."""
    return _engine("PBI_DB_USER", "PBI_DB_PASSWORD", erp_db_name())
