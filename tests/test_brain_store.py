"""Decision lifecycle rules (pure; no database)."""
import datetime as dt

import pandas as pd

from kkj.brain.store import reconcile

TODAY = dt.date(2026, 11, 5)


def existing(rows):
    return pd.DataFrame(rows, columns=["action_key", "status", "snoozed_until"])


def test_lifecycle():
    ex = existing([
        ("still_open", "open", None),
        ("gone_open", "open", None),
        ("gone_approved", "approved", None),
        ("back_resolved", "resolved", None),
        ("back_done", "done", None),
        ("snooze_over", "snoozed", dt.date(2026, 11, 1)),
        ("snooze_on", "snoozed", dt.date(2026, 11, 20)),
        ("gone_snoozed", "snoozed", dt.date(2026, 11, 20)),
        ("said_no", "rejected", None),
        ("gone_rejected", "rejected", None),
    ])
    cands = [{"action_key": k} for k in
             ("still_open", "back_resolved", "back_done", "snooze_over", "snooze_on", "said_no", "brand_new", "brand_new")]
    ops = reconcile(ex, cands, TODAY)
    assert [c["action_key"] for c in ops["create"]] == ["brand_new"]            # duplicates collapse
    assert {c["action_key"] for c in ops["update"]} == {"still_open", "back_resolved", "back_done", "snooze_over",
                                                         "snooze_on", "said_no"}
    assert sorted(ops["reopen"]) == ["back_done", "back_resolved"]
    assert ops["snooze_end"] == ["snooze_over"]
    assert sorted(ops["resolve"]) == ["gone_open", "gone_snoozed"]
    assert ops["done"] == ["gone_approved"]                                    # rejected ones are left alone


def test_first_run_creates_everything():
    ops = reconcile(existing([]), [{"action_key": "a"}, {"action_key": "b"}], TODAY)
    assert len(ops["create"]) == 2 and not any(ops[k] for k in ("update", "reopen", "resolve", "done"))
