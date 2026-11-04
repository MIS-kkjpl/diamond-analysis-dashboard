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


@pytest.mark.parametrize("path", ["/recommendations", "/recommendations?group=transfer", "/recommendations?view=closed",
                                  "/overview", "/checks", "/status", "/recommendations/summary"])
def test_pages_render(path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "Traceback" not in r.text


def test_home_redirects_and_old_links_work():
    assert client.get("/", follow_redirects=False).headers["location"] == "/recommendations"
    assert client.get("/decisions/12", follow_redirects=False).headers["location"] == "/recommendations/12"


def test_feed_filters_and_paging():
    r = client.get("/recommendations/feed", params={"group": "transfer", "offset": 0})
    assert r.status_code == 200 and "Transfer" in r.text
    r2 = client.get("/recommendations/feed", params={"group": "transfer", "offset": 25})
    assert r2.status_code == 200


def test_detail_and_exports():
    with settings.app_engine().connect() as c:
        aid = c.execute(text("SELECT action_id FROM brain.action WHERE kind = 'transfer' LIMIT 1")).scalar()
    assert client.get(f"/recommendations/{aid}").status_code == 200
    assert client.get("/recommendations/999999999").status_code == 404
    for path in ["/exports/transfers.xlsx", "/exports/offers.xlsx", "/exports/buying.xlsx", f"/exports/recommendation/{aid}.xlsx"]:
        r = client.get(path)
        assert r.status_code == 200 and r.content[:2] == b"PK", path


def test_response_needs_a_name_then_records_and_is_undone():
    with settings.app_engine().connect() as c:
        aid = c.execute(text("SELECT action_id FROM brain.action WHERE status = 'open' LIMIT 1")).scalar()
    assert client.post(f"/recommendations/{aid}/approve").status_code == 400    # no name, nothing recorded
    r = TestClient(app, cookies={"who": "pytest"}).post(f"/recommendations/{aid}/approve")
    try:
        assert r.status_code == 200 and "APPROVED" in r.text.upper() and 'id="summary"' in r.text
    finally:
        with settings.etl_engine().begin() as c:                                 # undo
            c.execute(text("DELETE FROM brain.action_event WHERE by = 'pytest'"))
            c.execute(text("""UPDATE brain.action SET status = 'open', decided_at = NULL, decided_by = NULL,
                              decision_note = NULL WHERE action_id = :a"""), {"a": aid})


@pytest.mark.parametrize("path", ["/inventory", "/inventory?branch=KJ2+%2F+Branch+Dharampeth&old_days=180",
                                  "/sales", "/sales?start=2026-10-01&end=2026-10-31&group=Diamond+Jewellery",
                                  "/insights", "/insights?group=&group=D+Kundan+Jewellery", "/settings"])
def test_analytics_and_settings_pages_render(path):
    r = client.get(path)
    assert r.status_code == 200, r.text[-800:]
    assert "Traceback" not in r.text and "NaT" not in r.text


def test_settings_save_rule_and_row_then_revert():
    me = TestClient(app, cookies={"who": "pytest"})
    with settings.app_engine().connect() as c:
        aged = c.execute(text("SELECT value FROM config.setting WHERE key = 'aged_days'")).scalar()
        person = c.execute(text("SELECT sales_code, name, note FROM config.salesperson LIMIT 1")).one()
    targets = {"target_turn": "1.0", "overstock_days": "365", "understock_days": "60", "aged_days": "400",
               "max_offer_discount_pct": "15"}
    try:
        refused = client.post("/settings/rules/Targets", data=targets).text                 # no name: refused
        assert "alert-danger" in refused and "Saved:" not in refused
        r = me.post("/settings/rules/Targets", data=targets)
        assert r.status_code == 200 and "Saved: A piece is aged after" in r.text
        from kkj import rules as R
        with settings.app_engine().connect() as c:
            now = R.load(c)
        demand = [(r.key, v) for r in R.RULES if r.section == "Demand"
                  for v in (now[r.key] if r.kind == "list" and r.choices else [", ".join(map(str, now[r.key]))]
                            if r.kind == "list" else [now[r.key]])]
        form = {}
        for k, v in demand:
            form.setdefault(k, []).append(str(v))
        form["price_band_edges"] = ["100000, 50000"]
        bad = me.post("/settings/rules/Demand", data=form)
        assert "smallest to largest" in bad.text and "Saved:" not in bad.text
        assert "enter a value" in me.post("/settings/rules/Demand", data={}).text
        r = me.post(f"/settings/salesperson/{person.sales_code}",
                    data={"name": person.name, "kind": "salesperson", "active": "on", "note": "pytest note"})
        assert r.status_code == 200 and "pytest note" in r.text
    finally:
        with settings.etl_engine().begin() as c:
            c.execute(text("UPDATE config.setting SET value = CAST(:v AS jsonb), updated_by = 'default' WHERE key = 'aged_days'"),
                      {"v": str(aged)})
            c.execute(text("UPDATE config.salesperson SET note = :n WHERE sales_code = :c"), {"n": person.note, "c": person.sales_code})
            c.execute(text("DELETE FROM config.change_log WHERE changed_by = 'pytest'"))
