"""
AI pages (kkj.ai): Ask Stockwise, the weekly brief, the AI second look on Recommendations and season notes on
Seasonality. Each exists only while its switch is on (kkj.features); the middleware in app.py hides the rest.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kkj.ai import ask as ask_mod
from kkj.ai import brief as brief_mod
from kkj.ai import client as ai
from kkj.ai import review as review_mod
from kkj.ai import season as season_mod
from kkj.web.common import render, templates, who

router = APIRouter()


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    recent = ask_mod.recent(limit=15)
    return render(request, "ask.html", "ask", recent=recent.to_dict("records"), names=ai.share_names())


@router.post("/ask", response_class=HTMLResponse)
def ask_post(request: Request, question: str = Form(...)):
    """Answers one question; returns the answer card (HTMX puts it at the top of the list)."""
    name = who(request)
    try:
        qid, answer, lookups = ask_mod.ask(question, name)
        row = {"question_id": qid, "asked_by": name, "question": question.strip(), "answer_md": answer,
               "lookups": lookups, "asked_at": None, "cost_usd": None}
        return templates.TemplateResponse(request, "_answer.html", {"r": row, "fresh": True})
    except ai.AIError as e:
        return templates.TemplateResponse(request, "_answer.html", {"r": {"question": question, "error": str(e)}, "fresh": True})


@router.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request, error: str = ""):
    briefs = brief_mod.latest("brief", 12)
    return render(request, "brief.html", "brief", briefs=briefs.to_dict("records"), error=error)


@router.post("/brief")
def brief_write(request: Request):
    try:
        brief_mod.write(who(request))
        return RedirectResponse("/brief", status_code=303)
    except ai.AIError as e:
        return RedirectResponse("/brief?" + urlencode({"error": str(e)}), status_code=303)


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
