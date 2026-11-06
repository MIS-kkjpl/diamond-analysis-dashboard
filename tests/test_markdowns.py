import pandas as pd

from kkj import rules as rules_mod
from kkj.brain import generators as gen

DEFAULTS = {r.key: r.default for r in rules_mod.RULES}


def test_default_schedule_matches_the_old_fixed_steps():
    for age, pct in [(200, 5), (365, 5), (366, 10), (730, 10), (731, 15), (1000, 15)]:
        assert gen.suggested_discount(age, DEFAULTS) == pct


def test_schedule_comes_from_settings_and_is_capped():
    r = {**DEFAULTS, "markdown_age_steps": [400, 600, 900], "markdown_pct_steps": [4, 8, 12, 25], "max_offer_discount_pct": 20}
    assert [gen.suggested_discount(a, r) for a in (300, 500, 700, 1000)] == [4, 8, 12, 20]   # 25 capped at 20


FESTS = pd.DataFrame({"name": ["Diwali"], "day": [pd.Timestamp("2026-11-08")]})


def _offer(key):
    return {"kind": "offer", "action_key": key, "summary": "Mark down.", "priority": 1.0, "evidence": {}}


def test_hold_window():
    assert gen.festival_hold(FESTS, "2026-10-17", DEFAULTS) is None                 # 22 days before: no hold yet
    assert gen.festival_hold(FESTS, "2026-10-18", DEFAULTS)[0] == "Diwali"          # 21 days before
    assert gen.festival_hold(FESTS, "2026-11-11", DEFAULTS)[0] == "Diwali"          # festival buying ends 3 days after
    assert gen.festival_hold(FESTS, "2026-11-12", DEFAULTS) is None
    assert gen.festival_hold(FESTS, "2026-11-06", {**DEFAULTS, "markdown_hold_days": 0}) is None   # 0 = off


def test_hold_drops_new_markdowns_and_marks_existing_ones():
    actions = [_offer("offer|KJ2|D TOPS"), _offer("offer|KJ3|D RING"), {"kind": "transfer", "action_key": "t", "priority": 1}]
    kept = gen.hold_markdowns(actions, FESTS, "2026-11-06", DEFAULTS, existing_keys={"offer|KJ2|D TOPS"})
    keys = [a["action_key"] for a in kept]
    assert keys == ["offer|KJ2|D TOPS", "t"]                                         # the new KJ3 markdown is not raised
    held = kept[0]
    assert held["summary"].startswith("On hold for Diwali") and held["priority"] < 1 and "festival hold" in held["evidence"]
    assert gen.hold_markdowns(actions[:1], FESTS, "2026-12-01", DEFAULTS, set()) == actions[:1]   # outside the window


def test_overlapping_festivals_make_one_hold_to_the_last():
    fests = pd.DataFrame({"name": ["Dhanteras", "Diwali"], "day": pd.to_datetime(["2026-11-06", "2026-11-08"])})
    name, last = gen.festival_hold(fests, "2026-11-06", DEFAULTS)
    assert name == "Dhanteras and Diwali" and last == pd.Timestamp("2026-11-08")
    held = gen.hold_markdowns([_offer("k")], fests, "2026-11-06", DEFAULTS, {"k"})[0]
    assert held["summary"].startswith("On hold for Dhanteras and Diwali") and "until after 11 Nov" in held["summary"]
