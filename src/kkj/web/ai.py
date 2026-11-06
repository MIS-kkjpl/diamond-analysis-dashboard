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
from kkj.web.common import templates, who

router = APIRouter()


# ── Ask Stockwise (side panel) ────────────────────────────────────────────────
@router.get("/ask")
def ask_page():
    return RedirectResponse("/recommendations?ask=1", status_code=303)


@router.get("/ask/panel", response_class=HTMLResponse)
def ask_panel(request: Request, c: str = ""):
    """The panel's body: a conversation (the latest one unless c is given, or a new one with c=new)."""
    name = who(request)
    past = ask_mod.conversations(name)
    cid = None if c == "new" else (int(c) if c.isdigit() else (past[0]["conversation_id"] if past else None))
    rows = ask_mod.conversation(cid, name) if cid else []
    return templates.TemplateResponse(request, "_ask_body.html", {"rows": rows, "cid": cid if rows else "", "past": past,
                                                                   "names": ai.share_names()})


@router.post("/ask", response_class=HTMLResponse)
def ask_post(request: Request, question: str = Form(...), conversation_id: str = Form(""), page: str = Form(""),
             title: str = Form("")):
    """Starts a question; returns its bubble (polling until the answer is ready) and the conversation id."""
    name = who(request)
    try:
        context = ask_mod.describe_page(page, title)
        qid, cid = ask_mod.start(question, name, int(conversation_id) if conversation_id.isdigit() else None, context)
        r = ask_mod.get(qid, name)
        return templates.TemplateResponse(request, "_ask_bubble.html", {"r": r, "cid": cid, "oob": True})
    except ai.AIError as e:
        return templates.TemplateResponse(request, "_ask_bubble.html",
                                          {"r": {"question": question, "status": "failed", "answer_md": str(e)}})


@router.get("/ask/q/{qid}", response_class=HTMLResponse)
def ask_poll(request: Request, qid: int):
    r = ask_mod.get(qid, who(request))
    if r is None:
        return Response(status_code=404)
    return templates.TemplateResponse(request, "_ask_bubble.html", {"r": r})


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
