"""
KKJ Diamond Analytics: the web app (FastAPI + Jinja2 templates + HTMX + Tabler).

Reads and writes through dashboard_app (writes only brain and config). Pages are plain
HTML templates in web/templates; HTMX swaps parts of a page (a card, the list, the
summary) so actions don't reload everything.

Run from the project folder:
  .venv\\Scripts\\uvicorn kkj.web.app:app --host 0.0.0.0 --port 8600
"""
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from kkj.web import admin, analytics, exports, pages, recommendations
from kkj.web.common import HERE, status, templates

app = FastAPI(title="KKJ Diamond Analytics", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(recommendations.router)
app.include_router(pages.router)
app.include_router(exports.router)
app.include_router(analytics.router)
app.include_router(admin.router)


@app.get("/")
def home():
    return RedirectResponse("/recommendations")


@app.get("/status", response_class=HTMLResponse)
def live_status(request: Request):
    """The live badges in the top bar (polled every minute)."""
    return templates.TemplateResponse(request, "_status.html", {"status": status()})


@app.post("/me")
def set_name(name: str = Form(default="")):
    """Remember who is using this browser (recorded with approvals and notes). No password: there are no logins yet."""
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    response.set_cookie("who", name.strip()[:60], max_age=60 * 60 * 24 * 365, samesite="lax")
    return response
