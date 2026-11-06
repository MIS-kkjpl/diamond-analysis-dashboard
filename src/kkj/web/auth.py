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
- A password set for someone else (Settings -> Users) is temporary: until they choose
  their own, every page sends them to Account.

Accounts are managed in Settings -> Users, or from the command line: python -m kkj.web.users
"""
import hashlib
import json
import secrets
from urllib.parse import quote, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from kkj import settings
from kkj.web import roles
from kkj.web.common import engine, render, templates

COOKIE = "stockwise_session"
IDLE_HOURS = float(settings.optional("SESSION_IDLE_HOURS", "12"))
MAX_DAYS = float(settings.optional("SESSION_MAX_DAYS", "7"))
SECURE_COOKIE = settings.optional("COOKIE_SECURE", "false").lower() == "true"   # true once served over HTTPS
LOCK_AFTER, LOCK_MINUTES = 5, 15
MIN_PASSWORD = 8                                      # NIST SP 800-63B minimum
PUBLIC = ("/login", "/static/")
WHILE_TEMPORARY = ("/account", "/logout", "/status")  # all a temporary password allows
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
            SELECT u.user_id, u.username, u.display_name, u.must_change_password, u.role
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
        if user["must_change_password"] and not path.startswith(WHILE_TEMPORARY):
            if request.headers.get("hx-request"):
                return Response(status_code=403, headers={"HX-Redirect": "/account"})
            return RedirectResponse("/account", status_code=303)
        request.state.user = user
        if not roles.allowed(user, request.method, path):
            return _refused(request, user)
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")     # no company data left in the browser cache
    return response


def _refused(request, user):
    """A request the person's role doesn't allow (web/roles.py)."""
    msg = f"Your role ({roles.LABELS.get(user.get('role'), user.get('role'))}) can't do this. Ask an admin if you need it."
    if request.headers.get("hx-request"):
        return Response(msg, status_code=403, headers={"HX-Trigger": json.dumps({"sw-refused": msg})})
    from kkj.web.common import render
    return render(request, "refused.html", "", message=msg)


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
    if new == current:
        return render(request, "account.html", "account", error="Choose a password different from the current one.",
                      saved=False)
    with engine().begin() as conn:
        conn.execute(text("""UPDATE auth.app_user SET password_hash = :h, password_changed_at = now(),
                             must_change_password = false WHERE user_id = :id"""),
                     {"h": hash_password(new), "id": me["user_id"]})
        # signed out everywhere else
        conn.execute(text("DELETE FROM auth.session WHERE user_id = :id AND token_hash <> :t"),
                     {"id": me["user_id"], "t": token})
        _log(conn, me["username"], me["user_id"], True, "password_changed", _ip(request))
    request.state.user = {**me, "must_change_password": False}
    return render(request, "account.html", "account", error=None, saved=True)


# ── Settings -> Users: any signed-in person manages accounts (no roles) ─────────
def _users_ctx(request, error=None, saved=None):
    users = conn_rows("""
        SELECT u.user_id, u.username, u.display_name, u.active, u.must_change_password, u.role,
               u.locked_until > now() AS locked, u.last_login_at AT TIME ZONE 'Asia/Kolkata' AS last_login_at,
               (SELECT count(*) FROM auth.session s WHERE s.user_id = u.user_id AND s.expires_at > now()) AS sessions
        FROM auth.app_user u ORDER BY u.active DESC, u.display_name""")
    attempts = conn_rows("""SELECT at AT TIME ZONE 'Asia/Kolkata' AS at, username, success, reason, ip
                            FROM auth.login_event ORDER BY event_id DESC LIMIT 25""")
    return {"users": users, "attempts": attempts, "me": request.state.user["user_id"], "error": error, "saved": saved,
            "min_password": MIN_PASSWORD, "roles": roles.ROLES[::-1], "role_labels": roles.LABELS, "role_help": roles.HELP}


def conn_rows(sql, **params):
    with engine().connect() as conn:
        return [r._asdict() for r in conn.execute(text(sql), params)]


def _users_card(request, error=None, saved=None):
    return templates.TemplateResponse(request, "_users.html", _users_ctx(request, error, saved))


def _record(conn, by, username, old, new):
    """Account changes go into the settings change history (never a password)."""
    conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value)
                         VALUES (:by, 'user', :k, CAST(:old AS jsonb), CAST(:new AS jsonb))"""),
                 {"by": by, "k": username, "old": json.dumps(old), "new": json.dumps(new)})


@router.get("/settings/users", response_class=HTMLResponse)
def users_card(request: Request):
    return _users_card(request)


@router.post("/settings/users", response_class=HTMLResponse)
def add_user(request: Request, username: str = Form(default=""), display_name: str = Form(default=""),
             password: str = Form(default=""), role: str = Form(default="manager")):
    username, display_name = username.strip(), " ".join(display_name.split())
    if not username or not username.replace(".", "").replace("_", "").replace("-", "").isalnum() or len(username) > 40:
        return _users_card(request, error="Username: letters, digits, '.', '_' or '-' only.")
    if not display_name:
        return _users_card(request, error="Enter the person's name; it is recorded with their approvals.")
    problem = password_problem(password, username)
    if problem:
        return _users_card(request, error=f"Temporary password: {problem}")
    if role not in roles.ROLES:
        return _users_card(request, error="Choose a role.")
    by = request.state.user["display_name"]
    with engine().begin() as conn:
        if conn.execute(text("SELECT 1 FROM auth.app_user WHERE lower(username) = lower(:u)"), {"u": username}).first():
            return _users_card(request, error=f"{username} already exists.")
        conn.execute(text("""INSERT INTO auth.app_user (username, display_name, password_hash, must_change_password, role)
                             VALUES (:u, :n, :h, true, :r)"""),
                     {"u": username, "n": display_name, "h": hash_password(password), "r": role})
        _record(conn, by, username, None, {"added": display_name, "role": role})
    return _users_card(request, saved=f"Added {display_name}. Give them the temporary password; "
                                      "they choose their own at first sign-in.")


@router.post("/settings/users/{user_id}/{verb}", response_class=HTMLResponse)
def manage_user(request: Request, user_id: int, verb: str, password: str = Form(default=""),
                display_name: str = Form(default=""), role: str = Form(default="")):
    me = request.state.user
    with engine().connect() as conn:
        user = conn.execute(text("SELECT user_id, username, display_name, active, role FROM auth.app_user WHERE user_id = :id"),
                            {"id": user_id}).first()
        admins = conn.execute(text("SELECT count(*) FROM auth.app_user WHERE role = 'admin' AND active")).scalar()
    if user is None or verb not in ("password", "disable", "enable", "signout", "rename", "role"):
        return Response("Not found", status_code=404)
    if user_id == me["user_id"] and verb in ("password", "disable", "signout", "role"):
        return _users_card(request, error="For your own account use Account (top right); another admin can change your role.")
    if verb == "role" and role not in roles.ROLES:
        return _users_card(request, error="Choose a role.")
    if user.role == "admin" and user.active and admins <= 1 and (verb == "disable" or (verb == "role" and role != "admin")):
        return _users_card(request, error="Stockwise needs at least one admin. Make someone else admin first.")
    if verb == "password":
        problem = password_problem(password, user.username)
        if problem:
            return _users_card(request, error=f"Temporary password for {user.display_name}: {problem}")
    if verb == "rename" and not " ".join(display_name.split()):
        return _users_card(request, error="Enter a name.")
    sets = {"password": "password_hash = :h, password_changed_at = now(), must_change_password = true, "
                        "failed_logins = 0, locked_until = NULL",
            "disable": "active = false",
            "enable": "active = true, failed_logins = 0, locked_until = NULL",
            "rename": "display_name = :n",
            "role": "role = :r",
            "signout": None}[verb]
    with engine().begin() as conn:
        if sets:
            conn.execute(text(f"UPDATE auth.app_user SET {sets} WHERE user_id = :id"),
                         {"id": user_id, "h": hash_password(password) if verb == "password" else None,
                          "n": " ".join(display_name.split()), "r": role})
        if verb in ("password", "disable", "signout"):
            conn.execute(text("DELETE FROM auth.session WHERE user_id = :id"), {"id": user_id})
        _record(conn, me["display_name"], user.username, None,
                {"password": "temporary password set", "disable": "disabled", "enable": "enabled",
                 "signout": "signed out everywhere", "rename": f"name: {' '.join(display_name.split())}",
                 "role": f"role: {user.role} -> {role}"}[verb])
    done = {"password": f"Temporary password set for {user.display_name}; they choose their own at next sign-in.",
            "disable": f"{user.display_name} can no longer sign in.", "enable": f"{user.display_name} can sign in again.",
            "signout": f"{user.display_name} has been signed out everywhere.", "rename": "Name saved.",
            "role": f"{user.display_name} is now {roles.LABELS.get(role, role)}."}[verb]
    return _users_card(request, saved=done)
