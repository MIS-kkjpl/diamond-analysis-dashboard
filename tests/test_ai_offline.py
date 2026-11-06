"""The AI features end to end with a stand-in for the Claude API (no cost): they read the real data, build their
requests and store their results. Catches errors in our own code, like a name clash, before anyone clicks a button."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from kkj import settings
from kkj.ai import brief, client, review, season


def _fake_call(reply):
    calls = []

    def call(feature, who, **kwargs):
        calls.append((feature, kwargs))
        assert isinstance(kwargs["system"], list) and kwargs["system"][0]["type"] == "text"   # business notes ride along
        msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=reply(kwargs))], model="fake", usage=None)
        return msg, None
    return call, calls


@pytest.fixture
def cleanup():
    yield
    with settings.etl_engine().begin() as c:
        c.execute(text("DELETE FROM ai.text WHERE created_by = 'pytest-offline'"))


def test_brief_writes_and_stores(monkeypatch, cleanup):
    call, calls = _fake_call(lambda kw: "## The week in one line\nA test brief.")
    monkeypatch.setattr(client, "call", call)
    tid = brief.write("pytest-offline")
    assert calls[0][0] == "ai_brief" and "Facts for this week's brief" in calls[0][1]["messages"][0]["content"]
    assert brief.latest("brief", 1).iloc[0]["text_id"] == tid


def test_season_notes_write_and_store(monkeypatch, cleanup):
    call, calls = _fake_call(lambda kw: "## The next 90 days\n- test")
    monkeypatch.setattr(client, "call", call)
    tid = season.write("pytest-offline")
    assert calls[0][0] == "ai_season" and brief.latest("season", 1).iloc[0]["text_id"] == tid


def test_second_look_runs_and_keeps_only_real_recommendations(monkeypatch):
    with settings.app_engine().connect() as c:
        open_id = c.execute(text("SELECT action_id FROM brain.action WHERE status = 'open' ORDER BY priority DESC LIMIT 1")).scalar()
    if open_id is None:
        pytest.skip("no open recommendations")
    call_id = client._log("pytest", "pytest-offline", "fake", None, 0.0)       # a real call-log row for the notes to point at
    reply = json.dumps({"notes": [{"type": "caution", "action_ids": [open_id, 999999999], "note": "pytest note"},
                                  {"type": "duplicate", "action_ids": [999999999], "note": "only an unknown id"}]})

    def call(feature, who, **kwargs):
        assert feature == "ai_review" and isinstance(kwargs["system"], list)
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)], model="fake", usage=None), call_id
    monkeypatch.setattr(client, "call", call)
    try:
        review_id, n = review.run("pytest-offline")
        assert n == 1                                            # the note about an unknown recommendation only is dropped
        with settings.app_engine().connect() as c:
            rows = c.execute(text("SELECT note, action_ids FROM ai.review_note WHERE review_id = :r"), {"r": review_id}).all()
        assert [(r.note, list(r.action_ids)) for r in rows] == [("pytest note", [open_id])]
    finally:
        with settings.etl_engine().begin() as c:
            c.execute(text("DELETE FROM ai.review_note WHERE call_id = :c"), {"c": call_id})
            c.execute(text("DELETE FROM ai.call_log WHERE call_id = :c"), {"c": call_id})
