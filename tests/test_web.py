"""Web app routes against the real database (read-only, except one decision that is undone)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kkj import settings
from kkj.web.app import app

client = TestClient(app)


def _has_decisions():
    with settings.app_engine().connect() as c:
        return c.execute(text("SELECT count(*) FROM brain.action")).scalar() > 0


pytestmark = pytest.mark.skipif(not _has_decisions(), reason="no decisions stored (run python -m kkj.brain.run)")


@pytest.mark.parametrize("path", ["/decisions", "/decisions?group=transfer", "/decisions?view=closed",
                                  "/overview", "/checks", "/status", "/decisions/summary"])
def test_pages_render(path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "Traceback" not in r.text


def test_home_redirects_to_decisions():
    assert client.get("/", follow_redirects=False).headers["location"] == "/decisions"


def test_feed_filters_and_paging():
    r = client.get("/decisions/feed", params={"group": "transfer", "offset": 0})
    assert r.status_code == 200 and "Transfer" in r.text
    r2 = client.get("/decisions/feed", params={"group": "transfer", "offset": 25})
    assert r2.status_code == 200


def test_detail_and_exports():
    with settings.app_engine().connect() as c:
        aid = c.execute(text("SELECT action_id FROM brain.action WHERE kind = 'transfer' LIMIT 1")).scalar()
    assert client.get(f"/decisions/{aid}").status_code == 200
    assert client.get("/decisions/999999999").status_code == 404
    for path in ["/exports/transfers.xlsx", "/exports/offers.xlsx", "/exports/buying.xlsx", f"/exports/decision/{aid}.xlsx"]:
        r = client.get(path)
        assert r.status_code == 200 and r.content[:2] == b"PK", path


def test_decision_needs_a_name_then_records_and_is_undone():
    with settings.app_engine().connect() as c:
        aid = c.execute(text("SELECT action_id FROM brain.action WHERE status = 'open' LIMIT 1")).scalar()
    assert client.post(f"/decisions/{aid}/approve").status_code == 400          # no name, no decision
    r = TestClient(app, cookies={"who": "pytest"}).post(f"/decisions/{aid}/approve")
    try:
        assert r.status_code == 200 and "APPROVED" in r.text.upper() and 'id="summary"' in r.text
    finally:
        with settings.etl_engine().begin() as c:                                 # undo
            c.execute(text("DELETE FROM brain.action_event WHERE by = 'pytest'"))
            c.execute(text("""UPDATE brain.action SET status = 'open', decided_at = NULL, decided_by = NULL,
                              decision_note = NULL WHERE action_id = :a"""), {"a": aid})
