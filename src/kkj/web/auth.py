"""
Sign-in for Stockwise: one account per person, no roles (everyone signed in sees everything).

- Passwords: Argon2id (argon2-cffi defaults, OWASP's first choice); only the hash is stored.
- Sessions: a random token in an HttpOnly, SameSite=Lax cookie. The database keeps only its
  SHA-256, so the session table can't be replayed. Expires after SESSION_IDLE_HOURS without
  use (the pages' 60-second polling doesn't count) or SESSION_MAX_DAYS in all.
- Lock-out: LOCK_AFTER wrong passwords in a row lock the account for LOCK_MINUTES. The
  message never says whether the username or the password was wrong.
- Every page except /login and /static needs a session. POSTs from another site are
  refused (Origin check, on top of SameSite).
- Every sign-in attempt is logged in auth.login_event.

Accounts are added and reset from the command line: python -m kkj.web.users
"""
import hashlib
import secrets
from urllib.parse import quote, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from kkj import settings
from kkj.web.common import engine, render, templates

COOKIE = "stockwise_session"
IDLE_HOURS = float(settings.optional("SESSION_IDLE_HOURS", "12"))
MAX_DAYS = float(settings.optional("SESSION_MAX_DAYS", "7"))
SECURE_COOKIE = settings.optional("COOKIE_SECURE", "false").lower() == "true"   # true once served over HTTPS
LOCK_AFTER, LOCK_MINUTES = 5, 15
MIN_PASSWORD = 10
PUBLIC = ("/login", "/static/")
POLLS = ("/status", "/recommendations/summary")      # background refreshes: don't keep a session alive

hasher = PasswordHasher()
_DUMMY = hasher.hash(secrets.token_hex(16))           # checked for unknown usernames so timing gives nothing away


def hash_password(password):
    return hasher.hash(password)


def password_problem(password, username=""):
    """None if acceptable. Length only, as NIST SP 800-63B advises (no forced symbols)."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    if username and password.lower() == username.lower():
        return "The password can't be the username."
    return None


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _ip(request):
    return request.client.host if request.client else None


def _log(conn, username, user_id, success, reason, ip):
    conn.execute(text("""INSERT INTO auth.login_event (username, user_id, success, reason, ip)
                         VALUES (:u, :id, :s, :r, :ip)"""),
                 {"u": username[:100], "id": user_id, "s": success, "r": reason, "ip": ip})


def check_login(username, password, ip):
    """(user row, None) on success, else (None, message). Records the attempt."""
    username = username.strip()
    with engine().begin() as conn:
        user = conn.execute(text("""SELECT user_id, username, display_name, password_hash, active,
                                           failed_logins, locked_until > now() AS locked
                                    FROM auth.app_user WHERE lower(username) = lower(:u)"""), {"u": username}).first()
        wrong = "The username or password is incorrect."
        if user is None:
            try:
                hasher.verify(_DUMMY, password)
            except VerificationError:
                pass
            _log(conn, username, None, False, "unknown_user", ip)
            return None, wrong
        if user.locked:
            _log(conn, username, user.user_id, False, "locked", ip)
            return None, f"Too many wrong attempts. Try again in {LOCK_MINUTES} minutes."
        try:
            hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError, VerificationError):
            failed = user.failed_logins + 1
            lock = failed >= LOCK_AFTER
            conn.execute(text(f"""UPDATE auth.app_user SET failed_logins = :f,
                                  locked_until = CASE WHEN :lock THEN now() + interval '{LOCK_MINUTES} minutes' END
                                  WHERE user_id = :id"""),
                         {"f": 0 if lock else failed, "lock": lock, "id": user.user_id})
            _log(conn, username, user.user_id, False, "bad_password", ip)
            return None, wrong
        if not user.active:
            _log(conn, username, user.user_id, False, "inactive", ip)
            return None, "This account is switched off. Ask the administrator."
        new_hash = hasher.hash(password) if hasher.check_needs_rehash(user.password_hash) else user.password_hash
        conn.execute(text("""UPDATE auth.app_user SET failed_logins = 0, locked_until = NULL, last_login_at = now(),
                             password_hash = :h WHERE user_id = :id"""), {"h": new_hash, "id": user.user_id})
        _log(conn, username, user.user_id, True, None, ip)
    return user, None


def start_session(user_id, request):
    token = secrets.token_urlsafe(32)
    with engine().begin() as conn:
        conn.execute(text(f"""INSERT INTO auth.session (token_hash, user_id, expires_at, ip, user_agent)
                              VALUES (:t, :u, now() + interval '{MAX_DAYS} days', :ip, :ua)"""),
                     {"t": _digest(token), "u": user_id, "ip": _ip(request),
                      "ua": (request.headers.get("user-agent") or "")[:300]})
        conn.execute(text("DELETE FROM auth.session WHERE expires_at < now()"))      # tidy up
    return token


def session_user(token, touch=True):
    """The signed-in user for a cookie token, or None. Refreshes last_seen_at unless touch is False."""
    if not token:
        return None
    with engine().begin() as conn:
        row = conn.execute(text(f"""
            SELECT u.user_id, u.username, u.display_name
            FROM auth.session s JOIN auth.app_user u USING (user_id)
            WHERE s.token_hash = :t AND u.active AND s.expires_at > now()
              AND s.last_seen_at > now() - interval '{IDLE_HOURS} hours'"""), {"t": _digest(token)}).first()
        if row and touch:
            conn.execute(text("UPDATE auth.session SET last_seen_at = now() WHERE token_hash = :t"), {"t": _digest(token)})
    return row._asdict() if row else None


def end_session(token):
    if token:
        with engine().begin() as conn:
            conn.execute(text("DELETE FROM auth.session WHERE token_hash = :t"), {"t": _digest(token)})


def _same_origin(request):
    """False if a state-changing request comes from another site (OWASP: verify Origin / Referer)."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True             # non-browser clients; browsers always send Origin on POST
    return urlsplit(source).netloc == request.headers.get("host")


def _safe_next(target):
    """Only redirect to a path on this site after signing in."""
    return target if target and target.startswith("/") and not target.startswith("//") and "\\" not in target \
        else "/recommendations"


async def middleware(request: Request, call_next):
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return Response("Refused: this request came from another site.", status_code=403)
    if not path.startswith(PUBLIC):
        user = await run_in_threadpool(session_user, request.cookies.get(COOKIE), path not in POLLS)
        if user is None:
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        request.state.user = user
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")     # no company data left in the browser cache
    return response


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    if session_user(request.cookies.get(COOKIE), touch=False):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None, "username": ""})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(default=""), password: str = Form(default=""), next: str = Form(default="")):
    user, error = check_login(username, password, _ip(request))
    if error:
        return templates.TemplateResponse(request, "login.html",
                                          {"next": _safe_next(next), "error": error, "username": username.strip()},
                                          status_code=401)
    end_session(request.cookies.get(COOKIE))              # never reuse a token from before sign-in
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(COOKIE, start_session(user.user_id, request), max_age=int(MAX_DAYS * 86400),
                        httponly=True, samesite="lax", secure=SECURE_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(request: Request):
    end_session(request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return render(request, "account.html", "account", error=None, saved=False)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(request: Request, current: str = Form(default=""), new: str = Form(default=""),
                    confirm: str = Form(default="")):
    me = request.state.user
    with engine().connect() as conn:
        stored = conn.execute(text("SELECT password_hash FROM auth.app_user WHERE user_id = :id"),
                              {"id": me["user_id"]}).scalar()
    try:
        hasher.verify(stored, current)
        error = None
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        error = "The current password is incorrect."
    error = error or ("The new passwords don't match." if new != confirm else password_problem(new, me["username"]))
    if error:
        return render(request, "account.html", "account", error=error, saved=False)
    token = _digest(request.cookies.get(COOKIE) or "")
    with engine().begin() as conn:
        conn.execute(text("""UPDATE auth.app_user SET password_hash = :h, password_changed_at = now()
                             WHERE user_id = :id"""), {"h": hash_password(new), "id": me["user_id"]})
        # signed out everywhere else
        conn.execute(text("DELETE FROM auth.session WHERE user_id = :id AND token_hash <> :t"),
                     {"id": me["user_id"], "t": token})
        _log(conn, me["username"], me["user_id"], True, "password_changed", _ip(request))
    return render(request, "account.html", "account", error=None, saved=True)
