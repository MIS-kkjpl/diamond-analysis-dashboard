"""
Stockwise: inventory and sales intelligence, the web app (FastAPI + Jinja2 templates + HTMX + Tabler).

Reads and writes through dashboard_app (writes only brain and config). Pages are plain
HTML templates in web/templates; HTMX swaps parts of a page (a card, the list, the
summary) so actions don't reload everything. Every page needs a signed-in account
(web/auth.py; accounts are managed with python -m kkj.web.users).

Run from the project folder:
  .venv\\Scripts\\uvicorn kkj.web.app:app --host 0.0.0.0 --port 8600
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from kkj import features
from kkj.web import admin, ai, analytics, auth, exports, lookup, pages, recommendations, sellers
from kkj.web.common import HERE, status, templates

app = FastAPI(title="Stockwise", docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.middleware)


@app.middleware("http")
async def switched_off(request: Request, call_next):
    """Pages of a feature switched off in .env don't exist (kkj.features)."""
    if features.blocked(request.url.path):
        return PlainTextResponse("Not found", status_code=404)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(auth.router)
app.include_router(recommendations.router)
app.include_router(pages.router)
app.include_router(exports.router)
app.include_router(analytics.router)
app.include_router(admin.router)
app.include_router(lookup.router)
app.include_router(sellers.router)
app.include_router(ai.router)


@app.get("/")
def home():
    return RedirectResponse("/recommendations")


@app.get("/status", response_class=HTMLResponse)
def live_status(request: Request):
    """The live badges in the top bar (polled every minute)."""
    return templates.TemplateResponse(request, "_status.html", {"status": status()})

