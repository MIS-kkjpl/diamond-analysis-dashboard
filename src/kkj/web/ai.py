"""
AI parts of the app (kkj.ai). Each exists only while its switch is on (kkj.features); the middleware in app.py
hides the rest.

  Ask Stockwise   a side panel on every page (base.html): /ask/panel loads a conversation, POST /ask starts a
                  question, /ask/q/<id> is polled until its answer is ready
  Weekly brief    a folded card at the top of Recommendations; POST /brief writes one, /brief/<id>/print prints it
  AI second look  POST /recommendations/ai-review (notes shown on Recommendations)
  Season notes    POST /seasonality/notes (card on Seasonality)
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kkj.ai import ask as ask_mod
from kkj.ai import brief as brief_mod
from kkj.ai import client as ai
from kkj.ai import review as review_mod
from kkj.ai import season as season_mod
from kkj.web import roles
from kkj.web.common import templates, who

router = APIRouter()


def _user(request):
    """The signed-in account (user_id, username, display_name); chats belong to it, not to a display name."""
    return request.state.user


# ── Ask Stockwise (side panel) ────────────────────────────────────────────────
@router.get("/ask")
def ask_page():
    return RedirectResponse("/recommendations?ask=1", status_code=303)


@router.get("/ask/panel", response_class=HTMLResponse)
def ask_panel(request: Request, c: str = ""):
    """The panel's body: a conversation (the latest one unless c is given, or a new one with c=new)."""
    uid = _user(request)["user_id"]
    past = ask_mod.conversations(uid)
    cid = None if c == "new" else (int(c) if c.isdigit() else (past[0]["conversation_id"] if past else None))
    rows = ask_mod.conversation(cid, uid) if cid else []
    return templates.TemplateResponse(request, "_ask_body.html", {"rows": rows, "cid": cid if rows else "", "past": past,
                                                                   "names": ai.share_names()})


@router.post("/ask", response_class=HTMLResponse)
def ask_post(request: Request, question: str = Form(...), conversation_id: str = Form(""), page: str = Form(""),
             title: str = Form("")):
    """Starts a question; returns its bubble (polling until the answer is ready) and the conversation id."""
    user = _user(request)
    try:
        context = ask_mod.describe_page(page, title)
        qid, cid = ask_mod.start(question, user, int(conversation_id) if conversation_id.isdigit() else None, context)
        r = ask_mod.get(qid, user["user_id"])
        return templates.TemplateResponse(request, "_ask_bubble.html", {"r": r, "cid": cid, "oob": True})
    except ai.AIError as e:
        return templates.TemplateResponse(request, "_ask_bubble.html",
                                          {"r": {"question": question, "status": "failed", "answer_md": str(e)}})


@router.get("/ask/q/{qid}", response_class=HTMLResponse)
def ask_poll(request: Request, qid: int):
    r = ask_mod.get(qid, _user(request)["user_id"])
    if r is None:
        return Response(status_code=404)
    return templates.TemplateResponse(request, "_ask_bubble.html", {"r": r})


@router.post("/ask/q/{qid}/note/{index}", response_class=HTMLResponse)
def ask_save_note(request: Request, qid: int, index: int):
    """Save a business note the AI offered in an answer; returns the updated bubble."""
    user = _user(request)
    try:
        ask_mod.save_proposed(qid, index, user)
    except ValueError as e:
        return Response(str(e), status_code=400)
    return templates.TemplateResponse(request, "_ask_bubble.html", {"r": ask_mod.get(qid, user["user_id"])})


# ── Business notes (Settings tab) ─────────────────────────────────────────────
def _notes_card(request, error=None, saved=None):
    from kkj.ai import notes
    return templates.TemplateResponse(request, "_notes.html", {
        "rows": notes.active().to_dict("records"), "admin": roles.at_least(_user(request).get("role"), "admin"),
        "error": error, "saved": saved})


@router.get("/settings/notes", response_class=HTMLResponse)
def notes_card(request: Request):
    return _notes_card(request)


@router.post("/settings/notes", response_class=HTMLResponse)
def notes_add(request: Request, note: str = Form("")):
    from kkj.ai import notes
    try:
        notes.add(note, _user(request)["display_name"])
        return _notes_card(request, saved="Note added. The AI takes it into account from the next answer.")
    except ValueError as e:
        return _notes_card(request, error=str(e))


@router.post("/settings/notes/{note_id}", response_class=HTMLResponse)
def notes_change(request: Request, note_id: int, note: str = Form(""), verb: str = Form("save")):
    from kkj.ai import notes
    user = _user(request)
    if not roles.at_least(user.get("role"), "admin"):
        return _notes_card(request, error="Only an admin can edit or remove a note.")
    try:
        if verb == "remove":
            notes.remove(note_id, user["display_name"])
            return _notes_card(request, saved="Note removed.")
        notes.edit(note_id, note, user["display_name"])
        return _notes_card(request, saved="Note saved.")
    except ValueError as e:
        return _notes_card(request, error=str(e))


@router.get("/settings/ai-usage", response_class=HTMLResponse)
def ai_usage(request: Request):
    """AI spend by person and feature (admins only; others get nothing)."""
    from sqlalchemy import text
    from kkj.ai import notes
    from kkj.web.common import engine
    if not roles.at_least(_user(request).get("role"), "admin"):
        return HTMLResponse("")
    per_user, budget = ask_mod.limits()
    today = "(at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date"
    with engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(text(f"""
            SELECT asked_by, feature, count(*) AS calls, count(*) FILTER (WHERE {today}) AS today, coalesce(sum(cost_usd), 0) AS usd
            FROM ai.call_log WHERE at > now() - interval '30 days' GROUP BY 1, 2 ORDER BY usd DESC""")).mappings()]
        tot = conn.execute(text(f"""SELECT coalesce(sum(cost_usd) FILTER (WHERE {today}), 0), coalesce(sum(cost_usd), 0),
                                           count(*), count(*) FILTER (WHERE NOT ok)
                                    FROM ai.call_log WHERE at > now() - interval '30 days'""")).first()
    labels = {"ai_ask": "Ask Stockwise", "ai_brief": "Weekly brief", "ai_review": "AI second look", "ai_season": "Season notes"}
    return templates.TemplateResponse(request, "_ai_usage.html", {
        "rows": rows, "labels": labels, "today": float(tot[0]), "month": float(tot[1]), "calls": tot[2], "failed": tot[3],
        "budget": budget, "per_user": per_user})


# ── Weekly brief (card on Recommendations) ────────────────────────────────────
@router.get("/brief")
def brief_page():
    return RedirectResponse("/recommendations#brief", status_code=303)


@router.post("/brief")
def brief_write(request: Request):
    try:
        brief_mod.write(who(request))
        return RedirectResponse("/recommendations?brief=open#brief", status_code=303)
    except ai.AIError as e:
        return RedirectResponse("/recommendations?" + urlencode({"brief_error": str(e)}) + "#brief", status_code=303)


@router.get("/brief/{text_id}/print", response_class=HTMLResponse)
def brief_print(request: Request, text_id: int):
    briefs = brief_mod.latest("brief", 60)
    hit = briefs[briefs["text_id"] == text_id]
    if hit.empty:
        return Response("No such brief", status_code=404)
    return templates.TemplateResponse(request, "brief_print.html", {"b": hit.iloc[0].to_dict()})


# ── AI second look, season notes ──────────────────────────────────────────────
@router.post("/recommendations/ai-review")
def review_run(request: Request):
    try:
        review_mod.run(who(request))
        return RedirectResponse("/recommendations#ai-notes", status_code=303)
    except ai.AIError as e:
        return RedirectResponse("/recommendations?" + urlencode({"ai_error": str(e)}) + "#ai-notes", status_code=303)


@router.post("/seasonality/notes")
def season_write(request: Request):
    try:
        season_mod.write(who(request))
        return RedirectResponse("/seasonality#season-notes", status_code=303)
    except ai.AIError as e:
        return RedirectResponse("/seasonality?" + urlencode({"ai_error": str(e)}) + "#season-notes", status_code=303)
