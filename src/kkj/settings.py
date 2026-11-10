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
SQL_FILES = ["schema.sql", "sales.sql", "stock_hist.sql", "config.sql", "brain.sql", "auth.sql", "ai.sql"]
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
# Earlier financial years are separate ERP programs with their own login (set with scripts/set_erp_year.py)
ERP_YEARS = {"2025-26": "54", "2024-25": "32", "2023-24": "22", "2022-23": "12", "2021-22": "12"}   # 2021-22 and 2022-23 share one program
# A program can hold more than one financial year; after login the ERP screen picks one with user/setFY.
# Program 12: id 9 = 2021-22 (what a login lands on), id 1 = 2022-23 (getFinYrFirmList).
ERP_FIN_YEARS = {"2022-23": "1", "2021-22": "9"}


def erp_login_settings(year=None):
    """Login for the current financial year, or for an earlier one (e.g. "2025-26") from ERP_<year>_* in .env.
    The year can also come from the ERP_YEAR environment variable, so any pipeline can run against an earlier year:
    set ERP_YEAR=2025-26 before `python -m kkj.pipelines.stock --as-on --date ...`."""
    year = year or os.environ.get("ERP_YEAR")
    if not year:
        return {
            "username": require("ERP_USERNAME"),
            "password": require("ERP_PASSWORD"),
            "x_key": require("ERP_API_KEY"),
            "role_code": optional("ERP_ROLE_CODE", "2665"),
        }
    tag = "ERP_" + year.replace("-", "_")
    return {
        "username": require(f"{tag}_USERNAME"),
        "password": require(f"{tag}_PASSWORD"),
        "x_key": require("ERP_API_KEY"),
        "role_code": optional(f"{tag}_ROLE_CODE", optional("ERP_ROLE_CODE", "2665")),
        "program_key": optional(f"{tag}_PROGRAM_KEY", ERP_YEARS.get(year, "")),
        "token": optional(f"{tag}_TOKEN", ""),      # a browser session token, used instead of logging in
        "fin_year": optional(f"{tag}_FIN_YEAR", ERP_FIN_YEARS.get(year, "")),   # see ERP_FIN_YEARS
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
