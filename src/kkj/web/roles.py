"""
Roles: who may change what. Three, the usual minimum for an internal decision tool:

  admin    everything: accounts, settings and business rules, editing or removing business notes, AI usage
  manager  acts on recommendations (approve, dismiss, snooze, complete, notes) and refreshes them; adjusts ideal
           stock (fixes with a reason, reviewed later); writes the
           weekly brief and season notes, runs the AI second look; adds business notes
  viewer   sees every page and can use Ask Stockwise; changes nothing

Checked on the server for every request (web/auth.py middleware calls allowed()); templates only hide what a
person's role can't do (can()). Anything that changes data and isn't listed below needs an admin, so a new
action is locked down until it is deliberately opened up.
"""
import re

ROLES = ("viewer", "manager", "admin")
LABELS = {"admin": "Admin", "manager": "Manager", "viewer": "Viewer"}
HELP = {"admin": "everything, including users and settings",
        "manager": "acts on recommendations, writes AI briefs and notes",
        "viewer": "sees everything and can use Ask Stockwise, but changes nothing"}
RANK = {r: i for i, r in enumerate(ROLES)}

# (method, path pattern, minimum role) for requests that change something; first match wins
RULES = [
    ("POST", r"^/(login|logout)$", "viewer"),
    ("POST", r"^/account/password$", "viewer"),
    ("POST", r"^/ask/q/\d+/note/\d+$", "manager"),          # saving a note the AI offered
    ("POST", r"^/ask$", "viewer"),
    ("POST", r"^/ask/c/\d+/delete$", "viewer"),            # people delete their own chats (checked: owner only)
    ("POST", r"^/recommendations/ai-review$", "manager"),
    ("POST", r"^/recommendations/\d+/[a-z]+$", "manager"),
    ("POST", r"^/refresh$", "manager"),
    ("POST", r"^/ideal-stock/adjust$", "manager"),          # fixing an ideal by hand (kkj.brain.fixes)
    ("POST", r"^/brief$", "manager"),
    ("POST", r"^/seasonality/notes$", "manager"),
    ("POST", r"^/settings/notes$", "manager"),
    ("GET", r"^/settings(/|$)", "admin"),                    # the Settings page and its parts
]
CHANGES = ("POST", "PUT", "PATCH", "DELETE")


def at_least(role, minimum):
    return RANK.get(role or "viewer", 0) >= RANK[minimum]


def required(method, path):
    """The minimum role for a request, or None when any signed-in person may make it."""
    for m, pattern, role in RULES:
        if method == m and re.match(pattern, path):
            return role
    return "admin" if method in CHANGES else None


def allowed(user, method, path):
    need = required(method, path)
    return need is None or at_least((user or {}).get("role"), need)


def can(request, minimum):
    """For templates: may the signed-in person do things that need this role?"""
    user = getattr(request.state, "user", None) if request is not None else None
    return bool(user) and at_least(user.get("role"), minimum)
