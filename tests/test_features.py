"""Feature switches (kkj.features) and the AI pages, without calling the Claude API."""
import pytest

from kkj import features


@pytest.fixture
def env(monkeypatch):
    def set_(**kv):
        for k, v in kv.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
    return set_


def test_defaults_and_switches(env):
    env(FEATURE_SALESPEOPLE=None, FEATURE_AI_ASK=None)
    assert features.on("salespeople") is True            # analytics pages default on
    assert features.on("ai_ask") is False                # AI defaults off
    env(FEATURE_SALESPEOPLE="off")
    assert features.on("salespeople") is False
    env(FEATURE_SALESPEOPLE="ON")
    assert features.on("salespeople") is True


def test_ai_needs_a_key(env):
    env(FEATURE_AI_ASK="on", ANTHROPIC_API_KEY="")
    assert features.on("ai_ask") is False
    env(ANTHROPIC_API_KEY="test-key")
    assert features.on("ai_ask") is True


def test_blocked_paths(env):
    env(FEATURE_OUTCOMES="off", FEATURE_SALESPEOPLE="on")
    assert features.blocked("/recommendations/outcomes").key == "outcomes"
    assert features.blocked("/recommendations/12") is None          # only the outcomes page, not every recommendation
    assert features.blocked("/salespeople/K0477") is None
    env(FEATURE_SALESPEOPLE="off")
    assert features.blocked("/salespeople/K0477").key == "salespeople"
    assert features.blocked("/sales") is None                       # /sales is not /salespeople


def test_markdown_never_runs_html():
    from kkj.web.common import markdown
    html = str(markdown("**bold** <script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
    assert "<strong>bold</strong>" in html and "<script>" not in html and "<table>" in html
