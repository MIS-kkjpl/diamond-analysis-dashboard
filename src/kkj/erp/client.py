"""
Acme Padm ERP session: login, POST helper, server messages, product-group lookups.

Shared by every ERP report (stock, sales). No report logic here.
Security: tokens, the X-key and passwords are never printed, logged or saved.
"""
import logging

import requests

HOST = "https://kothari.gold:8443"
SPROGRAM_KEY = "5"
TIMEOUT = 180            # seconds per report request; stock step A takes ~20 s
LOGIN_TIMEOUT = 30
VERIFY_SSL = True        # set False only if you get an SSL certificate error
CUSTOM_LOCALE = "Asia/Kolkata;en-US;dd/MM/yyyy;HH:mm:ss;dd/MM/yyyy HH:mm:ss"
JSON_CONTENT_TYPE = "application/json;charset=UTF-8"
JSON_HEADERS = {"Content-Type": JSON_CONTENT_TYPE}

if not VERIFY_SSL:
    import urllib3
    urllib3.disable_warnings()

log = logging.getLogger("kkj.erp")


class ErpError(Exception):
    """A failure with a message that is safe to show (no secrets in it)."""


class Unauthorized(ErpError):
    """HTTP 401/403: the token was rejected."""


def new_session(settings):
    """One Session for login and all report calls; carries the shared headers."""
    session = requests.Session()
    session.verify = VERIFY_SSL
    # Each financial year is its own ERP program (sProgramKey); the current year's is SPROGRAM_KEY
    session.program_key = str(settings.get("program_key") or SPROGRAM_KEY)
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "CustomLocale": CUSTOM_LOCALE,
        "X-key": settings["x_key"],
    })
    return session


def post(session, path, what, timeout=TIMEOUT, **kwargs):
    """POST to HOST/path, turning network failures into readable errors."""
    kwargs.setdefault("params", {"sProgramKey": getattr(session, "program_key", SPROGRAM_KEY)})
    try:
        return session.post(f"{HOST}/{path}", timeout=timeout, **kwargs)
    except requests.exceptions.SSLError:
        raise ErpError(f"SSL error during {what}. If the ERP certificate is the problem, "
                       "set VERIFY_SSL = False in kkj/erp/client.py.")
    except requests.exceptions.Timeout:
        raise ErpError(f"{what} timed out after {timeout} s. The server may be busy; try again.")
    except requests.exceptions.ConnectionError:
        raise ErpError(f"Could not connect to {HOST} during {what}. Check network/VPN.")


def server_messages(data):
    """Collect the ERP's own messages (resCol[].message, msg, error); never the stack trace."""
    if not isinstance(data, dict):
        return "no message"
    msgs = [c.get("message") for c in data.get("resCol") or [] if isinstance(c, dict)]
    msgs += [data.get("msg"), data.get("error")]
    return "; ".join(str(m) for m in msgs if m) or "no message"


# ── Login (port of acmeLogin in utils/acme-token.ts) ─────────────────────────
def login(session, settings):
    """Log in, store the token on the session, and return logCode (may be None)."""
    log.info("Logging in to ERP...")
    session.headers.pop("authorization", None)
    r = post(session, "acme-document-web/login", "login", timeout=LOGIN_TIMEOUT,
             params={"applicationCode": 1, "sProgramKey": getattr(session, "program_key", SPROGRAM_KEY),
                     "tenantCode": 1},
             # requests form-encodes a dict: username=...&password=...
             data={"username": settings["username"], "password": settings["password"]})
    if not r.ok:
        raise ErpError(f"Login failed: HTTP {r.status_code}. Check ERP_USERNAME/ERP_PASSWORD/ERP_API_KEY.")

    try:
        body = r.json()
    except ValueError:
        body = {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}

    # Token: response header first, then the body (same order as the reference)
    token = (r.headers.get("authorization") or data.get("token")
             or body.get("token") or data.get("authorization"))
    if not token:
        raise ErpError("Login succeeded but no token was found in the response.")
    session.headers["authorization"] = token

    # logCode identifies this login session; inventory reports expect it
    log_code = ((body.get("userLoginViewModel") or {}).get("logCode")
                or (data.get("userLoginViewModel") or {}).get("logCode"))

    # setRoleCode: needed for write actions, best-effort for reads (as in the reference)
    try:
        rr = post(session, "acme-document-web/user/setRoleCode", "setRoleCode",
                  timeout=LOGIN_TIMEOUT, data=str(settings["role_code"]),
                  headers=JSON_HEADERS)
        role_data = (rr.json() or {}).get("data") or {}
        if isinstance(role_data, dict):
            log_code = log_code or role_data.get("logCode") or role_data.get("logUserCode")
    except (ErpError, ValueError) as e:
        log.warning(f"  Warning: setRoleCode failed ({type(e).__name__}); continuing.")

    log.info("  Logged in." + ("" if log_code else " (no logCode returned)"))
    return log_code


# ── Product-group lookups ────────────────────────────────────────────────────
def item_codes_for_groups(session, product_codes):
    """Every item code in the given product groups, as the ERP lists them today.

    Reports need allItemCodes; fetching them live means new items added to a
    group are never silently left out (a hand-captured list goes stale).
    """
    codes = set()
    for group in product_codes:
        r = post(session, "acme-document-web/doc/getItemCodeListOfPrdGrp", "item code lookup",
                 timeout=LOGIN_TIMEOUT, headers=JSON_HEADERS,
                 json={"params": {"proSubChildList": [group], "onlySetItems": False}})
        if r.status_code in (401, 403):
            raise Unauthorized(f"Item code lookup: HTTP {r.status_code}")
        if not r.ok:
            raise ErpError(f"Item code lookup for group {group} failed: HTTP {r.status_code}.")
        codes.update((r.json().get("data") or {}).get("itemCodeList") or [])
    return sorted(codes)
