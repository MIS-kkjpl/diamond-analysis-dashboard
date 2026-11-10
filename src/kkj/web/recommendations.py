"""Recommendations: the inbox, one page per recommendation, and approve / dismiss / snooze / complete / note."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kkj.brain import store
from kkj.web.common import engine, read, render, templates, who

router = APIRouter()

KINDS = {  # kind -> (label, Tabler icon, badge colour)
    "transfer": ("Stock transfer", "ti-arrows-exchange", "blue"),
    "understock": ("Replenishment", "ti-package-import", "orange"),
    "offer": ("Markdown", "ti-tag", "purple"),
    "liquidate": ("Clearance", "ti-recycle", "red"),
    "supplier_slow": ("Supplier", "ti-building-factory-2", "yellow"),
    "supplier_fast": ("Supplier", "ti-building-factory-2", "green"),
    "pause_buying": ("Purchasing", "ti-shopping-cart-off", "cyan"),
    "buy_band": ("Purchasing", "ti-shopping-cart", "cyan"),
    "discount": ("Discount control", "ti-discount", "pink"),
    "turn_increase": ("Purchasing", "ti-trending-up", "green"),
    "turn_reduce": ("Purchasing", "ti-trending-down", "orange"),
    "turn_phase_out": ("Purchasing", "ti-trash", "red"),
    "ideal_review": ("Ideal review", "ti-hand-stop", "yellow"),
    "festival": ("Festival", "ti-calendar-event", "yellow"),
    "broken_set": ("Broken set", "ti-puzzle", "orange"),
}
GROUPS = {"all": ("All", None), "transfer": ("Transfers", ["transfer"]), "replenish": ("Replenishment", ["understock"]),
          "markdown": ("Markdowns", ["offer"]), "clearance": ("Clearance", ["liquidate"]),
          "supplier": ("Suppliers", ["supplier_slow", "supplier_fast"]),
          "purchasing": ("Purchasing", ["turn_increase", "turn_reduce", "turn_phase_out", "ideal_review"]),
          "festival": ("Festivals", ["festival"]),
          "sets": ("Broken sets", ["broken_set"]),
          "discount": ("Discounts", ["discount"])}
VIEWS = {"open": ("To review", ["open"]), "approved": ("Approved", ["approved"]), "snoozed": ("Snoozed", ["snoozed"]),
         "dismissed": ("Dismissed", ["rejected"]), "closed": ("Closed", ["done", "resolved"])}
STATUS = {"open": "To review", "approved": "Approved", "rejected": "Dismissed", "snoozed": "Snoozed",
          "done": "Completed", "resolved": "Resolved"}
EVENTS = {"created": "Raised", "approved": "Approved", "rejected": "Dismissed", "snoozed": "Snoozed",
          "done": "Completed", "reopened": "Reopened", "auto_resolved": "Resolved automatically (issue cleared)",
          "condition_cleared": "Completed (issue cleared after approval)", "snooze_ended": "Snooze ended",
          "comment": "Note"}
ACTIONS = {"approve": "approve", "dismiss": "reject", "snooze": "snooze", "complete": "done", "reopen": "reopen",
           "comment": "comment"}             # URL verb -> kkj.brain.store decision
PAGE = 25
SORTS = {"priority": ("Most important first", "priority DESC, impact_value DESC"),
         "money": ("Most money first", "impact_value DESC NULLS LAST"),
         "newest": ("Newest first", "first_seen_at DESC"),
         "branch": ("By branch", "split_part(coalesce(branch, 'zz'), ' / ', 1), priority DESC")}
COLUMNS = """action_id, kind, title, summary, branch, impact_value, impact_label, first_impact, confidence,
             evidence, pieces, status, times_seen, decided_by, decision_note, snoozed_until,
             decided_at AT TIME ZONE 'Asia/Kolkata' AS decided_at,
             first_seen_at AT TIME ZONE 'Asia/Kolkata' AS first_seen,
             last_seen_at AT TIME ZONE 'Asia/Kolkata' AS last_seen, priority"""
templates.env.globals.update(STATUS=STATUS, EVENTS=EVENTS)


def items(view="open", group="all", branch="", q="", limit=PAGE, offset=0, sort="priority"):
    """Recommendations matching the filters, best first, plus the total count."""
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
                    ORDER BY {SORTS.get(sort, SORTS["priority"])[1]} LIMIT :limit OFFSET :offset""",
                limit=limit, offset=offset, **params)
    return [_prep(r) for r in rows.to_dict("records")], total


def _prep(a):
    a["label"], a["icon"], a["colour"] = KINDS.get(a["kind"], (a["kind"], "ti-bolt", "secondary"))
    if not isinstance(a.get("branch"), str) or not a["branch"].strip():     # company-wide cards have no branch
        a["branch"] = None
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


@router.get("/recommendations", response_class=HTMLResponse)
def inbox(request: Request, view: str = "open", group: str = "all", branch: str = "", q: str = "",
          sort: str = "priority"):
    found, total = items(view, group, branch, q, sort=sort)
    return render(request, "recommendations.html", "recommendations", items=found, total=total, summary=summary(),
                  groups=GROUPS, views=VIEWS, branches=branches(),
                  f={"view": view, "group": group, "branch": branch, "q": q, "sort": sort}, page=PAGE, sorts=SORTS)


@router.get("/recommendations/feed", response_class=HTMLResponse)
def feed(request: Request, view: str = "open", group: str = "all", branch: str = "", q: str = "", offset: int = 0,
         sort: str = "priority"):
    """The list only (filters and 'show more' swap this part of the page)."""
    found, total = items(view, group, branch, q, offset=offset, sort=sort)
    return templates.TemplateResponse(request, "_feed.html", {
        "items": found, "total": total, "offset": offset, "page": PAGE, "who": who(request),
        "f": {"view": view, "group": group, "branch": branch, "q": q, "sort": sort}})


@router.get("/recommendations/summary", response_class=HTMLResponse)
def summary_part(request: Request):
    return templates.TemplateResponse(request, "_summary.html", {"summary": summary(), "oob": False})


@router.get("/recommendations/outcomes", response_class=HTMLResponse)
def outcomes_page(request: Request):
    """Every decided recommendation and what happened since (kkj.brain.outcomes)."""
    from kkj.brain import outcomes
    from kkj.web import charts
    with engine().connect() as conn:
        followed = outcomes.follow(conn)
    if followed.empty:
        return render(request, "outcomes.html", "recommendations", empty=True)
    kinds = outcomes.by_kind(followed).assign(kind=lambda d: d["kind"].map(lambda k: KINDS.get(k, (k,))[0]))
    rows = followed.sort_values("decided_at", ascending=False).assign(
        kind=lambda d: d["kind"].map(lambda k: KINDS.get(k, (k,))[0]),
        status=lambda d: d["status"].map(STATUS),
        title=lambda d: [(t, f"/recommendations/{i}") for t, i in zip(d["title"], d["action_id"])])
    return render(request, "outcomes.html", "recommendations", empty=False,
                  t_kinds=charts.table(kinds, {"recommendations": "num", "pieces": "num", "sold": "num", "sold_share": "pct",
                                               "sold_value": "money"}, {"sold_share": "share sold", "sold_value": "sold for"}),
                  t_rows=charts.table(rows[["title", "kind", "status", "decided_by", "decided_at", "days", "outcome", "sold_value"]],
                                      {"title": "link", "decided_at": "date", "days": "num", "sold_value": "money"},
                                      {"title": "recommendation", "decided_by": "by", "decided_at": "decided", "days": "days since",
                                       "sold_value": "sold for"}))


@router.get("/recommendations/{action_id}", response_class=HTMLResponse)
def detail(request: Request, action_id: int):
    rows = read(f"SELECT {COLUMNS} FROM brain.action WHERE action_id = :a", a=action_id)
    if rows.empty:
        return Response("No such recommendation", status_code=404)
    history = read("""SELECT at AT TIME ZONE 'Asia/Kolkata' AS at, by, event, note FROM brain.action_event
                      WHERE action_id = :a ORDER BY event_id DESC""", a=action_id).to_dict("records")
    follow, follow_pieces = None, []
    if rows.iloc[0]["status"] in ("approved", "done", "rejected"):
        from kkj.brain import outcomes
        with engine().connect() as conn:
            f = outcomes.follow(conn, [action_id])
            if len(f):
                follow = f.iloc[0].to_dict()
                p = outcomes.pieces(conn, f)
                follow_pieces = p.astype(object).where(p.notna(), None).to_dict("records") if len(p) else []
    from kkj import features
    ai_notes = []
    if features.on("ai_review"):
        from kkj.ai import review
        ai_notes = review.notes_for(action_id)
    a = _prep(rows.iloc[0].to_dict())
    adjust = {}                       # stock-level recommendations: adjust the ideal right here
    subject = read("SELECT subject FROM brain.action WHERE action_id = :a", a=action_id)["subject"].iloc[0] or {}
    subject = json.loads(subject) if isinstance(subject, str) else subject
    if a["kind"] in ("turn_increase", "turn_reduce", "turn_phase_out") and subject.get("group"):
        from kkj.web.analytics import adjust_ctx
        adjust = adjust_ctx("group", subject["group"])
    elif a["kind"] == "ideal_review" and subject.get("fix_key"):
        from kkj.web.analytics import adjust_ctx
        adjust = adjust_ctx(subject.get("fix_level", "group"), subject["fix_key"], branch=subject.get("fix_branch"))
    return render(request, "recommendation_detail.html", "recommendations", a=a,
                  history=history, follow=follow, follow_pieces=follow_pieces, ai_notes=ai_notes, **adjust)


@router.post("/recommendations/{action_id}/{verb}", response_class=HTMLResponse)
def act(request: Request, action_id: int, verb: str, note: str = Form(default="")):
    """Record a person's response; returns the updated card plus the refreshed summary."""
    name = who(request)
    if not name:
        return Response("Sign in first.", status_code=400)
    if verb not in ACTIONS:
        return Response("Unknown action", status_code=400)
    with engine().begin() as conn:
        store.decide(conn, action_id, ACTIONS[verb], name, note.strip() or None)
    a = _prep(read(f"SELECT {COLUMNS} FROM brain.action WHERE action_id = :a", a=action_id).iloc[0].to_dict())
    card = templates.get_template("_card.html").render(a=a, who=name, just_acted=True, request=request)
    oob = templates.get_template("_summary.html").render(summary=summary(), oob=True, request=request)
    return HTMLResponse(card + oob)


@router.post("/refresh")
def refresh():
    """Re-run every rule on the latest data; the page then reloads."""
    from kkj.brain.run import refresh as run_refresh
    run_refresh("dashboard")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.get("/decisions")
@router.get("/decisions/{rest:path}")
def old_links(rest: str = ""):
    """Links from before the rename keep working."""
    return RedirectResponse(f"/recommendations/{rest}".rstrip("/"), status_code=301)
