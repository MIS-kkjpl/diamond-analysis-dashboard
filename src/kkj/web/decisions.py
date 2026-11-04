"""Decision inbox, decision pages, and the approve / reject / snooze / done / note actions."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from kkj.brain import store
from kkj.web.common import engine, read, render, templates, who

router = APIRouter()

KINDS = {  # kind -> (label, Tabler icon, badge colour)
    "transfer": ("Transfer", "ti-arrows-exchange", "blue"),
    "understock": ("Running short", "ti-alert-triangle", "orange"),
    "offer": ("Offer", "ti-tag", "purple"),
    "liquidate": ("Liquidate", "ti-recycle", "red"),
    "supplier_slow": ("Supplier", "ti-building-factory-2", "yellow"),
    "supplier_fast": ("Supplier", "ti-building-factory-2", "green"),
    "pause_buying": ("Buying", "ti-shopping-cart-off", "cyan"),
    "buy_band": ("Buying", "ti-shopping-cart", "cyan"),
    "discount": ("Discount", "ti-discount", "pink"),
}
GROUPS = {"all": ("All", None), "transfer": ("Transfers", ["transfer"]), "short": ("Running short", ["understock"]),
          "offer": ("Offers", ["offer"]), "liquidate": ("Liquidate", ["liquidate"]),
          "supplier": ("Suppliers", ["supplier_slow", "supplier_fast"]),
          "buying": ("Buying", ["pause_buying", "buy_band"]), "discount": ("Discounts", ["discount"])}
VIEWS = {"open": ("To decide", ["open"]), "approved": ("Approved", ["approved"]), "snoozed": ("Snoozed", ["snoozed"]),
         "rejected": ("Rejected", ["rejected"]), "closed": ("Closed", ["done", "resolved"])}
PAGE = 25
COLUMNS = """action_id, kind, title, summary, branch, impact_value, impact_label, first_impact, confidence,
             evidence, pieces, status, times_seen, decided_by, decision_note, snoozed_until,
             decided_at AT TIME ZONE 'Asia/Kolkata' AS decided_at,
             first_seen_at AT TIME ZONE 'Asia/Kolkata' AS first_seen,
             last_seen_at AT TIME ZONE 'Asia/Kolkata' AS last_seen, priority"""


def actions(view="open", group="all", branch="", q="", limit=PAGE, offset=0):
    """Decisions matching the filters, best first, plus the total count."""
    statuses = VIEWS.get(view, VIEWS["open"])[1]
    kinds = GROUPS.get(group, GROUPS["all"])[1]
    where, params = ["status = ANY(:statuses)"], {"statuses": statuses}
    if kinds:
        where.append("kind = ANY(:kinds)"); params["kinds"] = kinds
    if branch:
        where.append("split_part(coalesce(branch, ''), ' / ', 1) = :branch"); params["branch"] = branch
    if q:
        where.append("(title ILIKE :q OR summary ILIKE :q)"); params["q"] = f"%{q}%"
    cond = " AND ".join(where)
    total = int(read(f"SELECT count(*) AS n FROM brain.action WHERE {cond}", **params)["n"].iloc[0])
    rows = read(f"""SELECT {COLUMNS} FROM brain.action WHERE {cond}
                    ORDER BY priority DESC, impact_value DESC LIMIT :limit OFFSET :offset""",
                limit=limit, offset=offset, **params)
    return [_prep(r) for r in rows.to_dict("records")], total


def _prep(a):
    a["label"], a["icon"], a["colour"] = KINDS.get(a["kind"], (a["kind"], "ti-bolt", "secondary"))
    for f in ("evidence", "pieces"):
        if isinstance(a[f], str):
            a[f] = json.loads(a[f])
    a["pieces"] = a["pieces"] or []
    a["change"] = (a["impact_value"] - a["first_impact"]) if a["first_impact"] and a["impact_value"] else 0
    return a


def summary():
    s = read("""SELECT count(*) FILTER (WHERE status = 'open') AS open,
                       count(*) FILTER (WHERE status = 'open' AND confidence = 'high') AS open_high,
                       count(*) FILTER (WHERE status = 'open' AND first_seen_at > now() - interval '24 hours') AS new_24h,
                       count(*) FILTER (WHERE status = 'approved') AS approved,
                       count(*) FILTER (WHERE status = 'snoozed') AS snoozed
                FROM brain.action""").iloc[0].to_dict()
    s["closed_7d"] = int(read("""SELECT count(*) AS n FROM brain.action_event WHERE event IN
                                 ('done', 'condition_cleared', 'auto_resolved') AND at > now() - interval '7 days'""")["n"].iloc[0])
    return s


def branches():
    return read("""SELECT DISTINCT split_part(branch, ' / ', 1) AS b FROM brain.action
                   WHERE branch IS NOT NULL ORDER BY 1""")["b"].tolist()


@router.get("/decisions", response_class=HTMLResponse)
def inbox(request: Request, view: str = "open", group: str = "all", branch: str = "", q: str = ""):
    items, total = actions(view, group, branch, q)
    return render(request, "decisions.html", "decisions", items=items, total=total, summary=summary(),
                  groups=GROUPS, views=VIEWS, branches=branches(),
                  f={"view": view, "group": group, "branch": branch, "q": q}, page=PAGE)


@router.get("/decisions/feed", response_class=HTMLResponse)
def feed(request: Request, view: str = "open", group: str = "all", branch: str = "", q: str = "", offset: int = 0):
    """The list only (filters and 'show more' swap this part of the page)."""
    items, total = actions(view, group, branch, q, offset=offset)
    return templates.TemplateResponse(request, "_feed.html", {
        "items": items, "total": total, "offset": offset, "page": PAGE, "who": who(request),
        "f": {"view": view, "group": group, "branch": branch, "q": q}})


@router.get("/decisions/summary", response_class=HTMLResponse)
def summary_part(request: Request):
    return templates.TemplateResponse(request, "_summary.html", {"summary": summary(), "oob": False})


@router.get("/decisions/{action_id}", response_class=HTMLResponse)
def detail(request: Request, action_id: int):
    rows = read(f"SELECT {COLUMNS} FROM brain.action WHERE action_id = :a", a=action_id)
    if rows.empty:
        return Response("No such decision", status_code=404)
    history = read("""SELECT at AT TIME ZONE 'Asia/Kolkata' AS at, by, event, note FROM brain.action_event
                      WHERE action_id = :a ORDER BY event_id DESC""", a=action_id).to_dict("records")
    return render(request, "decision_detail.html", "decisions", a=_prep(rows.iloc[0].to_dict()), history=history)


@router.post("/decisions/{action_id}/{decision}", response_class=HTMLResponse)
def decide(request: Request, action_id: int, decision: str, note: str = Form(default="")):
    """Record a person's decision; returns the updated card plus the refreshed summary."""
    name = who(request)
    if not name:
        return Response("Type your name at the top first.", status_code=400)
    if decision not in (*store.DECISIONS, "comment"):
        return Response("Unknown decision", status_code=400)
    with engine().begin() as conn:
        store.decide(conn, action_id, decision, name, note.strip() or None)
    a = _prep(read(f"SELECT {COLUMNS} FROM brain.action WHERE action_id = :a", a=action_id).iloc[0].to_dict())
    card = templates.get_template("_card.html").render(a=a, who=name, just_decided=decision)
    oob = templates.get_template("_summary.html").render(summary=summary(), oob=True)
    return HTMLResponse(card + oob)


@router.post("/refresh")
def refresh():
    """Re-run every decision rule on the latest data; the page then reloads."""
    from kkj.brain.run import refresh as run_refresh
    result = run_refresh("dashboard")
    return Response(status_code=204, headers={
        "HX-Refresh": "true",
        "HX-Trigger": json.dumps({"toast": f"{result['created']} new · {result['updated']} updated · "
                                           f"{result['resolved']} closed"})})
