"""Settings: business rules by section, the salesperson and branch lists, and the change history."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj.web.common import engine, read, render, templates, who

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
            "branch_names": _branch_names(), "saved": saved, "error": error}


def _plain(df):
    """Missing values as None (never 'NaT'/'nan' in the inputs)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _people():
    return _plain(read("""SELECT sales_code, name, employee_code, branch, kind, active, left_on, match_status, note
                   FROM config.salesperson ORDER BY (match_status = 'matched'), name"""))


def _branches():
    return _plain(read("""SELECT erp_branch, short_name, hr_code, place, kind, active FROM config.branch
                          ORDER BY kind, short_name"""))


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
    if rule.choices or key == "transfer_excluded_branches":
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
                                          {"s": _section_ctx(section, error="Type your name at the top first."), "who": name})
    form = await request.form()
    try:
        values = {r.key: _parse(r, form) for r in rules_mod.RULES if r.section == section}
        for r in rules_mod.RULES:
            if r.key in values and r.min is not None and isinstance(values[r.key], (int, float)) \
                    and not (r.min <= values[r.key] <= (r.max if r.max is not None else float("inf"))):
                raise ValueError(f"{r.label}: must be between {r.min:g} and {r.max:g}")
    except (ValueError, KeyError) as e:
        return templates.TemplateResponse(request, "_rule_section.html", {"s": _section_ctx(section, error=str(e)), "who": name})
    with engine().begin() as conn:
        changed = [k for k, v in values.items() if rules_mod.save(conn, k, v, name)]
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
        return None, "Type your name at the top first."
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
