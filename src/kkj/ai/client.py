"""
The one way Stockwise calls Claude: every AI feature goes through call(), which

  - uses the model in AI_MODEL (default claude-opus-5-5) with the server-side refusal fallback,
  - streams the reply (long briefs never hit a request timeout) and returns the final message,
  - logs who asked, for which feature, the tokens and an estimated cost in ai.call_log,
  - never logs the API key or the prompt.

.env:  ANTHROPIC_API_KEY=...        (required; without it every AI feature stays off)
       ANTHROPIC_WORKSPACE_ID=wrkspc_...  (only for a key that is not tied to one workspace)
       AI_MODEL=claude-opus-5-5     (optional)
       AI_SHARE_NAMES=off           (optional; on = salespeople's names are sent, off = only their codes)
"""
import json
import time

import anthropic
from sqlalchemy import text

from kkj import settings

MODEL = settings.optional("AI_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# $ per million tokens: input, output, cache read, cache write (5-minute cache = 1.25 x input)
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
          "claude-opus-5": (5.0, 25.0, 0.50, 6.25), "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}


class AIError(Exception):
    """A readable reason an AI feature could not answer (shown to the person who asked)."""


def share_names():
    return settings.optional("AI_SHARE_NAMES", "off").strip().lower() in ("on", "true", "1", "yes")


_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is None:
        ws = settings.optional("ANTHROPIC_WORKSPACE_ID", "").strip()
        _CLIENT = anthropic.Anthropic(max_retries=3, timeout=600,
                                      default_headers={"anthropic-workspace-id": ws} if ws else None)
    return _CLIENT


def cost(model, usage):
    p = PRICES.get(model, PRICES["claude-opus-5-5"])
    return ((usage.input_tokens or 0) * p[0] + (usage.output_tokens or 0) * p[1]
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p[2]
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p[3]) / 1e6


def _log(feature, who, model, usage, seconds, ok=True, error=None):
    with settings.app_engine().begin() as conn:
        return conn.execute(text("""
            INSERT INTO ai.call_log (feature, asked_by, model, input_tokens, output_tokens, cache_read_tokens,
                                     cache_write_tokens, cost_usd, seconds, ok, error)
            VALUES (:f, :w, :m, :i, :o, :cr, :cw, :c, :s, :ok, :e) RETURNING call_id"""),
            {"f": feature, "w": who, "m": model,
             "i": getattr(usage, "input_tokens", None), "o": getattr(usage, "output_tokens", None),
             "cr": getattr(usage, "cache_read_input_tokens", None),
             "cw": getattr(usage, "cache_creation_input_tokens", None),
             "c": round(cost(model, usage), 4) if usage else None, "s": round(seconds, 2), "ok": ok,
             "e": (error or "")[:500] or None}).scalar_one()


class Totals:
    """Token totals across the calls of one multi-step answer (Ask Stockwise's lookup loop)."""
    def __init__(self):
        self.input_tokens = self.output_tokens = self.cache_read_input_tokens = self.cache_creation_input_tokens = 0

    def add(self, u):
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0


def create(**kwargs):
    """One streamed request with the refusal fallback; returns the final message. Raises AIError."""
    try:
        with client().beta.messages.stream(model=MODEL, betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as s:
            msg = s.get_final_message()
    except anthropic.AuthenticationError:
        raise AIError("The Claude API key was refused. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.PermissionDeniedError:
        raise AIError("This API key is not allowed to use the model. Check the key's workspace in the Claude Console.")
    except anthropic.RateLimitError:
        raise AIError("The Claude API is busy or the spending limit was reached. Try again in a minute.")
    except anthropic.BadRequestError as e:
        if "workspace" in str(e.message).lower():
            raise AIError("This API key needs a workspace: add ANTHROPIC_WORKSPACE_ID=wrkspc_... to .env (Claude Console > "
                          "Settings > Workspaces), or use a key created inside a workspace, then restart the app.")
        raise AIError(f"The request was not accepted: {e.message}")
    except anthropic.APIStatusError as e:
        raise AIError(f"The Claude API returned an error ({e.status_code}). Try again later.")
    except anthropic.APIConnectionError:
        raise AIError("Could not reach the Claude API from this computer. Check the internet connection.")
    if msg.stop_reason == "refusal":
        raise AIError("Claude declined to answer this one.")
    return msg


def call(feature, who, **kwargs):
    """create() plus logging. Returns (message, call_id)."""
    t0 = time.time()
    try:
        msg = create(**kwargs)
    except AIError as e:
        _log(feature, who, MODEL, None, time.time() - t0, ok=False, error=str(e))
        raise
    return msg, _log(feature, who, msg.model, msg.usage, time.time() - t0)


def log_totals(feature, who, model, totals, seconds, ok=True, error=None):
    return _log(feature, who, model, totals, seconds, ok, error)


def text_of(msg):
    return "\n".join(b.text for b in msg.content if b.type == "text").strip()


def as_json(obj):
    """Compact JSON for prompts: stable key order (keeps prompt caching effective), dates as text."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def spend(days=30):
    """Calls and estimated $ over the last `days` days, by feature (for the CLI and the Settings note)."""
    import pandas as pd
    with settings.app_engine().connect() as conn:
        return pd.read_sql(text("""SELECT feature, count(*) AS calls, sum(cost_usd) AS usd, sum(input_tokens) AS input_tokens,
                                          sum(output_tokens) AS output_tokens, count(*) FILTER (WHERE NOT ok) AS failed
                                   FROM ai.call_log WHERE at > now() - make_interval(days => :d) GROUP BY 1 ORDER BY 1"""),
                           conn, params={"d": days})
