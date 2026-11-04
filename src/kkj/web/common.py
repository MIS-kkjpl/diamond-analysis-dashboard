"""
Shared pieces of the web app: database engine, templates and their filters, the current
user's name (a cookie; there are no logins yet), and the live status shown on every page.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

import pandas as pd
from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from kkj import settings
from kkj.viz import indian, money, pct

HERE = Path(__file__).resolve().parent
CLASSIC_URL = "http://{host}:8501"          # the Streamlit app, for pages not moved yet


@lru_cache
def engine():
    """dashboard_app: reads every schema, writes only brain and config."""
    return settings.app_engine()


def read(sql, **params):
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def who(request: Request):
    return (request.cookies.get("who") or "").strip()


def _date(value, fmt="%d %b %Y"):
    if value is None or (isinstance(value, float) and math.isnan(value)) or pd.isna(value):
        return "–"
    return pd.Timestamp(value).strftime(fmt)


def _ago(value):
    if value is None or pd.isna(value):
        return "–"
    delta = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None) - pd.Timestamp(value)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 24 * 60:
        return f"{minutes // 60} h ago"
    return f"{delta.days} d ago"


def _num(value, decimals=0):
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "–"
    return indian(float(value), decimals)


def flatten(value, prefix=""):
    """{'receiver': {'pcs': 15}} -> [('receiver · pcs', 15)] for evidence tables."""
    if isinstance(value, str):
        value = json.loads(value or "{}")
    rows = []
    for key, v in (value or {}).items():
        label = f"{prefix}{str(key).replace('_', ' ')}"
        if isinstance(v, dict):
            rows += flatten(v, f"{label} · ")
        elif isinstance(v, float):
            rows.append((label, "–" if not math.isfinite(v) else f"{v:,.2f}".rstrip("0").rstrip(".")))
        elif isinstance(v, list):
            rows.append((label, ", ".join(map(str, v))))
        else:
            rows.append((label, "–" if v is None else v))
    return rows


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters.update(money=money, pct=pct, num=_num, date=_date, ago=_ago,
                             short=lambda b: str(b).split(" / ")[0] if b else "All branches")
templates.env.globals.update(flatten=flatten)


def status():
    """The latest successful recommendation refresh (shown live on every page)."""
    run = read("""SELECT run_id, finished_at AT TIME ZONE 'Asia/Kolkata' AS finished, stock_as_of, sales_to,
                         created, resolved FROM brain.run_log WHERE status = 'success' ORDER BY run_id DESC LIMIT 1""")
    return run.iloc[0].to_dict() if len(run) else None


def render(request: Request, template, active, **context):
    host = request.url.hostname or "localhost"
    return templates.TemplateResponse(request, template, {
        "who": who(request), "active": active, "status": status(),
        "classic": CLASSIC_URL.format(host=host), **context})
