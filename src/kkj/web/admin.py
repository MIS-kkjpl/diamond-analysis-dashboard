"""Settings: business rules by section, the salesperson and branch lists, and the change history."""
import json
import math

import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj.web.common import clear_cache, engine, read, render, templates, who

router = APIRouter(prefix="/settings")
SECTIONS = list(dict.fromkeys(r.section for r in rules_mod.RULES))
PERSON_COLS = ["name", "employee_code", "branch", "kind", "active", "left_on", "note"]
BRANCH_COLS = ["short_name", "hr_code", "place", "kind", "active"]
PERSON_KINDS = ["salesperson", "owner", "generic"]
BRANCH_KINDS = ["showroom", "head_office", "wholesale", "exhibition", "other"]


def _branch_names():
    return read("SELECT erp_branch FROM config.branch ORDER BY erp_branch")["erp_branch"].tolist()


def _section_ctx(section, saved=None, error=None):
    with engine().connect() as conn:
        current = rules_mod.load(conn)
    return {"section": section, "rules": [r for r in rules_mod.RULES if r.section == section], "current": current,
            "branch_names": _branch_names(), "product_names": _product_names(), "location_names": _location_names(),
            "saved": saved, "error": error}


def _location_names():
    """Every location in the latest stock (for the out-of-stock locations picker)."""
    return read("""SELECT DISTINCT location FROM erp.stock_snapshot WHERE location IS NOT NULL
                   AND snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot) ORDER BY 1""")["location"].tolist()


def _product_names():
    """Every product name in the stock and sales loads (for the exclusions picker)."""
    return read("""SELECT item_name FROM erp.stock_snapshot WHERE item_name IS NOT NULL
                   UNION SELECT item_name FROM sales.v_sales_line WHERE item_name IS NOT NULL ORDER BY 1""")["item_name"].tolist()


def _plain(df):
    """Missing values as None (never 'NaT'/'nan' in the inputs)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _people():
    return _plain(read("""SELECT sales_code, name, employee_code, branch, kind, active, left_on, match_status, note
                   FROM config.salesperson ORDER BY (match_status = 'matched'), name"""))


def _branches():
    return _plain(read("""SELECT erp_branch, short_name, hr_code, place, kind, active FROM config.branch
                          ORDER BY kind, short_name"""))


# ── Settings -> Ideal stock by group (config.stock_param, config.ideal_stock_override) ──
STOCK_LEVELS = {"item_group": "Item group", "product": "Product", "group": "Brick group"}
FIX_LEVELS = {"group": "Brick group", "product": "Product"}
STOCK_PARAMS = {  # param -> (label, unit, rule it overrides, parse/validate)
    "choice_months": ("Hold for choice", "months of forecast demand", "choice_months"),
    "replacement_days": ("Replacement time", "days", "replacement_days"),
    "display_min": ("Display minimum", "pieces", "display_min_pcs"),
    "grade": ("Service grade", "A, B or C", None),
}


def _check_param(param, raw):
    """(stored text, None) or (None, error message)."""
    raw = (raw or "").strip()
    if param == "grade":
        return (raw.upper(), None) if raw.upper() in ("A", "B", "C") else (None, "Service grade: A, B or C.")
    limits = {"choice_months": (0, 60), "replacement_days": (1, 365), "display_min": (0, 50)}[param]
    try:
        v = float(raw)
    except ValueError:
        return None, f"{STOCK_PARAMS[param][0]}: a number."
    if not limits[0] <= v <= limits[1]:
        return None, f"{STOCK_PARAMS[param][0]}: between {limits[0]} and {limits[1]} {STOCK_PARAMS[param][1]}."
    return f"{v:g}", None


def _stock_ctx(error=None, saved=None, form=None):
    """Values set per group, fixed ideals, and pickers from the latest ideal stock run."""
    params = _plain(read("""SELECT param, level, key, value, note, updated_by, updated_at AT TIME ZONE 'Asia/Kolkata' AS updated_at
                            FROM config.stock_param ORDER BY param, CASE level WHEN 'item_group' THEN 1
                            WHEN 'product' THEN 2 ELSE 3 END, key"""))
    overrides = _plain(read("""
        WITH latest AS (SELECT * FROM brain.ideal_stock WHERE as_of = (SELECT max(as_of) FROM brain.ideal_stock)
                        AND scope <> 'Company')
        SELECT o.level, o.key, o.branch, o.pcs, o.reason, o.updated_by, o.updated_at AT TIME ZONE 'Asia/Kolkata' AS updated_at,
               o.review_on, o.calc_at_fix,
               (SELECT sum(l.ideal_pcs) FROM latest l
                WHERE (CASE o.level WHEN 'group' THEN l.brick_group ELSE l.item_name END) = o.key
                  AND (o.branch = '*' OR l.scope = o.branch)) AS applied
        FROM config.ideal_stock_override o ORDER BY o.level DESC, o.key, o.branch"""))
    latest = read("""SELECT scope, brick_group, item_name, item_class_group FROM brain.ideal_stock
                     WHERE as_of = (SELECT max(as_of) FROM brain.ideal_stock)""")
    company = latest[latest["scope"] == "Company"]
    keys = {"group": sorted(company["brick_group"].dropna().unique()),
            "product": sorted(company["item_name"].dropna().unique()),
            "item_group": sorted(company["item_class_group"].dropna().unique())}
    branches = sorted(latest.loc[latest["scope"] != "Company", "scope"].dropna().unique())
    with engine().connect() as conn:
        rules = rules_mod.load(conn)
    defaults = {k: (rules[v[2]] if v[2] else "by sales value") for k, v in STOCK_PARAMS.items()}
    return {"params": params, "overrides": overrides, "keys": keys, "branches": branches, "levels": STOCK_LEVELS,
            "fix_levels": FIX_LEVELS,
            "kinds": STOCK_PARAMS, "defaults": defaults, "error": error, "saved": saved,
            "form": form or {"param": "choice_months", "level": "product", "key": "", "value": ""}}


@router.get("/ideal-stock", response_class=HTMLResponse)
def stock_card(request: Request):
    return templates.TemplateResponse(request, "_stock_params.html", _stock_ctx())


@router.post("/ideal-stock", response_class=HTMLResponse)
async def save_stock_param(request: Request):
    form = await request.form()
    param, level = form.get("param", ""), form.get("level", "")
    key, note = (form.get("key") or "").strip(), (form.get("note") or "").strip() or None
    keep = {"param": param, "level": level, "key": key, "value": form.get("value", "")}
    def again(error=None, saved=None):
        return templates.TemplateResponse(request, "_stock_params.html", _stock_ctx(error, saved, keep))
    if param not in STOCK_PARAMS or level not in STOCK_LEVELS or not key:
        return again("Choose what to set, a level and what it applies to.")
    by = who(request)
    with engine().begin() as conn:
        old = conn.execute(text("SELECT value FROM config.stock_param WHERE param = :p AND level = :l AND key = :k"),
                           {"p": param, "l": level, "k": key}).scalar()
        if form.get("delete") == "1":
            conn.execute(text("DELETE FROM config.stock_param WHERE param = :p AND level = :l AND key = :k"),
                         {"p": param, "l": level, "k": key})
            new, msg = None, f"Removed {STOCK_PARAMS[param][0].lower()} for {key}; it now uses the next level up."
        else:
            new, error = _check_param(param, form.get("value"))
            if error:
                return again(error)
            conn.execute(text("""INSERT INTO config.stock_param (param, level, key, value, note, updated_by)
                                 VALUES (:p, :l, :k, :v, :n, :by)
                                 ON CONFLICT (param, level, key) DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note,
                                 updated_at = now(), updated_by = EXCLUDED.updated_by"""),
                         {"p": param, "l": level, "k": key, "v": new, "n": note, "by": by})
            msg = (f"{STOCK_LEVELS[level]} {key}: {STOCK_PARAMS[param][0].lower()} {new} {STOCK_PARAMS[param][1]}. "
                   "Ideals update at the next refresh.")
        conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value, note)
                             VALUES (:by, 'stock_param', :k, CAST(:old AS jsonb), CAST(:new AS jsonb), :note)"""),
                     {"by": by, "k": f"{param} / {level}: {key}", "old": json.dumps(old), "new": json.dumps(new),
                      "note": note})
    return templates.TemplateResponse(request, "_stock_params.html", _stock_ctx(saved=msg))


@router.post("/ideal-stock/override", response_class=HTMLResponse)
async def save_override(request: Request):
    from kkj.brain import fixes
    form = await request.form()
    level, key, branch = form.get("level", ""), (form.get("key") or "").strip(), (form.get("branch") or "").strip()
    with engine().begin() as conn:
        if form.get("delete") == "1":
            msg, error = fixes.remove(conn, level, key, branch, who(request)), None
        else:
            msg, error = fixes.save(conn, level, key, branch, form.get("pcs"), form.get("reason"), who(request),
                                    rules_mod.load(conn)["override_review_days"])
        if not error:
            fixes.apply_now(conn, rules_mod.load(conn))      # the pages show it straight away
    return templates.TemplateResponse(request, "_stock_params.html", _stock_ctx(error=error, saved=msg))


def _festivals_ctx(error=None, saved=None):
    rows = _plain(read("""SELECT name, day, note, updated_by FROM config.festival ORDER BY day DESC"""))
    names = sorted({r["name"] for r in rows})
    return {"festivals": rows, "names": names, "error": error, "saved": saved, "today": pd.Timestamp.today().date()}


@router.get("/festivals", response_class=HTMLResponse)
def festivals_card(request: Request):
    return templates.TemplateResponse(request, "_festivals.html", _festivals_ctx())


@router.post("/festivals", response_class=HTMLResponse)
async def save_festival(request: Request):
    form = await request.form()
    name = " ".join((form.get("name") or "").split())
    note = (form.get("note") or "").strip() or None
    try:
        day = pd.Timestamp(form.get("day") or "").date()
    except ValueError:
        day = None
    if not name or day is None:
        return templates.TemplateResponse(request, "_festivals.html", _festivals_ctx(error="Enter a festival name and a date."))
    by = who(request)
    with engine().begin() as conn:
        if form.get("delete") == "1":
            conn.execute(text("DELETE FROM config.festival WHERE name = :n AND day = :d"), {"n": name, "d": day})
            msg, new = f"Removed {name} on {day:%d %b %Y}.", None
        else:
            conn.execute(text("""INSERT INTO config.festival (name, day, note, updated_by) VALUES (:n, :d, :note, :by)
                                 ON CONFLICT (name, day) DO UPDATE SET note = EXCLUDED.note, updated_at = now(),
                                 updated_by = EXCLUDED.updated_by"""), {"n": name, "d": day, "note": note, "by": by})
            msg, new = f"Saved {name} on {day:%d %b %Y}. Seasons and festivals uses it from the next refresh.", str(day)
        conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value)
                             VALUES (:by, 'festival', :k, NULL, CAST(:new AS jsonb))"""),
                     {"by": by, "k": f"{name} {day}", "new": json.dumps(new)})
    return templates.TemplateResponse(request, "_festivals.html", _festivals_ctx(saved=msg))


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    people = _people()
    history = read("""SELECT changed_at AT TIME ZONE 'Asia/Kolkata' AS changed_at, changed_by, table_name, row_key,
                             old_value, new_value FROM config.change_log ORDER BY change_id DESC LIMIT 200""").to_dict("records")
    return render(request, "settings.html", "settings", sections=[_section_ctx(s) for s in SECTIONS], people=people,
                  to_review=sum(p["match_status"] in ("unmapped", "name_match") for p in people), branches=_branches(),
                  person_kinds=PERSON_KINDS, branch_kinds=BRANCH_KINDS, history=history)


def _parse(rule, form):
    """The value of one rule from the submitted form, in the rule's own type."""
    key = rule.key
    if rule.kind == "bool":
        return key in form
    if rule.kind in ("int", "number", "choice") and not form.get(key):
        raise ValueError(f"{rule.label}: enter a value")
    if rule.kind == "int":
        return int(float(form[key]))
    if rule.kind == "number":
        return float(form[key])
    if rule.kind == "choice":
        if form[key] not in rule.choices:
            raise ValueError(f"{rule.label}: choose one of {', '.join(rule.choices)}")
        return form[key]
    if rule.choices or key in ("transfer_excluded_branches", "excluded_products", "excluded_locations"):
        return [v for v in form.getlist(key) if v]
    items = [x.strip() for x in form.get(key, "").split(",") if x.strip()]
    if rule.default and all(isinstance(d, (int, float)) for d in rule.default):
        values = [float(x) if "." in x else int(x) for x in items]
        if values != sorted(values):
            raise ValueError(f"{rule.label}: list the edges from smallest to largest")
        return values
    return items


@router.post("/rules/{section}", response_class=HTMLResponse)
async def save_rules(request: Request, section: str):
    name = who(request)
    if section not in SECTIONS:
        return Response("Unknown section", status_code=404)
    if not name:
        return templates.TemplateResponse(request, "_rule_section.html",
                                          {"s": _section_ctx(section, error="Sign in first."), "who": name})
    form = await request.form()
    try:
        values = {r.key: _parse(r, form) for r in rules_mod.RULES if r.section == section}
        if "markdown_age_steps" in values or "markdown_pct_steps" in values:
            ages = values.get("markdown_age_steps", rules_mod.BY_KEY["markdown_age_steps"].default)
            pcts = values.get("markdown_pct_steps", rules_mod.BY_KEY["markdown_pct_steps"].default)
            if len(pcts) != len(ages) + 1:
                raise ValueError(f"Markdown percentages: give one more than there are age steps ({len(ages) + 1} for "
                                 f"{len(ages)} steps), youngest first")
            if any(p < 0 or p > 60 for p in pcts) or any(a <= 0 for a in ages):
                raise ValueError("Markdown steps: ages above 0 days, percentages between 0 and 60")
        for r in rules_mod.RULES:
            if r.key in values and r.min is not None and isinstance(values[r.key], (int, float)) \
                    and not (r.min <= values[r.key] <= (r.max if r.max is not None else float("inf"))):
                raise ValueError(f"{r.label}: must be between {r.min:g} and {r.max:g}")
    except (ValueError, KeyError) as e:
        return templates.TemplateResponse(request, "_rule_section.html", {"s": _section_ctx(section, error=str(e)), "who": name})
    with engine().begin() as conn:
        changed = [k for k, v in values.items() if rules_mod.save(conn, k, v, name)]
    if changed:
        clear_cache()                     # cached reads depend on settings (e.g. exclusions)
    msg = ("Saved: " + ", ".join(rules_mod.BY_KEY[k].label for k in changed)) if changed else "No changes."
    return templates.TemplateResponse(request, "_rule_section.html", {"s": _section_ctx(section, saved=msg), "who": name})


async def _save_row(request, table, key_col, key, cols, kinds):
    name = who(request)
    form = await request.form()
    before = read(f"SELECT * FROM config.{table} WHERE {key_col} = :k", k=key)
    if before.empty:
        return None, "Not found"
    old = {c: before[c].iloc[0] for c in cols}
    new = {c: form.get(c) or None for c in cols}
    new["active"] = "active" in form
    if new.get("kind") not in kinds:
        return None, "Unknown type"
    if not name:
        return None, "Sign in first."
    set_sql = ", ".join(f"{c} = :{c}" for c in cols)
    extra = ", match_status = CASE WHEN match_status = 'matched' THEN 'matched' ELSE 'manual' END" if table == "salesperson" else ""
    with engine().begin() as conn:
        conn.execute(text(f"UPDATE config.{table} SET {set_sql}{extra}, updated_at = now(), updated_by = :by "
                          f"WHERE {key_col} = :key"), {**new, "by": name, "key": key})
        conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value)
                             VALUES (:by, :t, :k, CAST(:old AS jsonb), CAST(:new AS jsonb))"""),
                     {"by": name, "t": table, "k": key, "old": json.dumps(old, default=str), "new": json.dumps(new, default=str)})
    return _plain(read(f"SELECT * FROM config.{table} WHERE {key_col} = :k", k=key))[0], None


@router.post("/salesperson/{code}", response_class=HTMLResponse)
async def save_person(request: Request, code: str):
    row, error = await _save_row(request, "salesperson", "sales_code", code, PERSON_COLS, PERSON_KINDS)
    if error:
        return Response(error, status_code=400)
    return templates.TemplateResponse(request, "_person_row.html",
                                      {"p": row, "kinds": PERSON_KINDS, "who": who(request), "saved": True})


@router.post("/branch/{erp_branch:path}", response_class=HTMLResponse)
async def save_branch(request: Request, erp_branch: str):
    row, error = await _save_row(request, "branch", "erp_branch", erp_branch, BRANCH_COLS, BRANCH_KINDS)
    if error:
        return Response(error, status_code=400)
    return templates.TemplateResponse(request, "_branch_row.html",
                                      {"b": row, "kinds": BRANCH_KINDS, "who": who(request), "saved": True})
