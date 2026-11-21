"""
Feature switches: which parts of Stockwise are switched on.

Set in .env (read when the app starts; restart it after a change), one line per feature you want to change:

    FEATURE_AI_ASK=on
    FEATURE_SALESPEOPLE=off

Anything not set keeps the default below. A switched-off feature disappears completely: its menu item, its
links and buttons, and its pages (they answer "not found"). Switches live in .env, not in Settings, so only
whoever runs the app can change them; people using the app can't turn a feature on.

AI features also need ANTHROPIC_API_KEY in .env; without a key they stay off whatever the switch says.

    python -m kkj.features          # what is on, what is off, and why
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    default: bool
    help: str
    ai: bool = False
    paths: tuple = ()          # URL prefixes that belong to the feature


FEATURES = [
    # ── AI (Claude) ──
    Feature("ai_ask", "Ask Stockwise", False, "Questions in plain language, answered from the database with its sources.",
            ai=True, paths=("/ask",)),
    Feature("ai_brief", "Weekly brief", False, "A one-page management brief written from the week's figures.",
            ai=True, paths=("/brief",)),
    Feature("ai_review", "AI second look", False, "Notes on the recommendation list: conflicts, timing, duplicates.",
            ai=True, paths=("/recommendations/ai-review",)),
    Feature("ai_season", "Season notes", False, "Plain-language notes on the festival and seasonal outlook.",
            ai=True, paths=("/seasonality/notes",)),
    # ── analytics pages ── (Recommendations, the home page, is always on)
    Feature("overview", "Overview", True, "Headline numbers and trends.", paths=("/overview",)),
    Feature("inventory", "Inventory health", True, "Stock by branch and age, sets, stale approvals, old stock.",
            paths=("/inventory",)),
    Feature("sales", "Sales", True, "Sales by month, branch, item and salesperson; returns.", paths=("/sales",)),
    Feature("salespeople", "Salespeople", True, "Salesperson scorecard and person pages.", paths=("/salespeople",)),
    Feature("outcomes", "Outcomes", True, "What happened after recommendations were decided.",
            paths=("/recommendations/outcomes",)),
    Feature("stock_categories", "Stock by category", True,
            "Stock in Business Studio's 7 categories: branches, weight, value, age, pattern, gender, supplier.",
            paths=("/stock-categories",)),
    Feature("stock_turn", "Stock turn", True, "Stock turn by brick and brick group.", paths=("/stock-turn",)),
    Feature("seasonality", "Seasonality", True, "Yearly pattern, festivals and the 90-day outlook.", paths=("/seasonality",)),
    Feature("insights", "Insights", True, "Ageing, suppliers, assortment and exhibitions.", paths=("/insights",)),
    # ── tools ──
    Feature("lookup", "Look up", True, "Search for a piece, product, salesperson or recommendation (top bar).",
            paths=("/lookup",)),
    Feature("checks", "Data quality", True, "Accuracy audit, unusual bills, load history.", paths=("/checks",)),
    Feature("exports", "Exports", True, "Excel downloads: transfer slips, markdown lists, purchasing, pieces.",
            paths=("/exports",)),
]
BY_KEY = {f.key: f for f in FEATURES}


def ai_key_present():
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def switch(key):
    """The .env setting for a feature: True / False, or None when not set."""
    raw = os.getenv(f"FEATURE_{key.upper()}", "").strip().lower()
    if raw in ("on", "true", "1", "yes"):
        return True
    if raw in ("off", "false", "0", "no"):
        return False
    return None


def on(key):
    """Is this feature switched on (and, for AI features, is there an API key)?"""
    f = BY_KEY[key]
    wanted = switch(key)
    enabled = f.default if wanted is None else wanted
    return enabled and (ai_key_present() if f.ai else True)


def any_ai():
    return any(on(f.key) for f in FEATURES if f.ai)


def blocked(path):
    """The feature that owns this URL, if that feature is off (longest matching prefix wins)."""
    hits = [(p, f) for f in FEATURES for p in f.paths if path == p or path.startswith(p + "/") or path.startswith(p + "?")]
    if not hits:
        return None
    _, f = max(hits, key=lambda h: len(h[0]))
    return None if on(f.key) else f


def status():
    """[(feature, on?, why)] for the command line."""
    rows = []
    for f in FEATURES:
        wanted = switch(f.key)
        why = "default" if wanted is None else f"FEATURE_{f.key.upper()}={'on' if wanted else 'off'}"
        if f.ai and (f.default if wanted is None else wanted) and not ai_key_present():
            why += ", but no ANTHROPIC_API_KEY"
        rows.append((f, on(f.key), why))
    return rows


if __name__ == "__main__":
    from kkj import settings  # noqa: F401  (loads .env)
    print(f"{'feature':14s} {'state':5s}  why")
    for f, state, why in status():
        print(f"{f.key:14s} {'ON' if state else 'off':5s}  {why}   ({f.label})")
