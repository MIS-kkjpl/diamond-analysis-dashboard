"""Web app routes against the real database (read-only, except one decision and some settings that are undone,
and temporary pytest accounts that are removed)."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kkj import settings
from kkj.web import auth
from kkj.web.app import app

USER, PASSWORD = "pytest", "pytest-password-123"
client = TestClient(app)          # signed in as the pytest account by the fixture below
anon = TestClient(app)            # never signed in


def _remove_test_accounts():
    with settings.etl_engine().begin() as c:
        c.execute(text("DELETE FROM auth.app_user WHERE username LIKE 'pytest%'"))
        c.execute(text("DELETE FROM auth.login_event WHERE username LIKE 'pytest%'"))


def _add_account(username, password):
    with settings.etl_engine().begin() as c:
        c.execute(text("INSERT INTO auth.app_user (username, display_name, password_hash) VALUES (:u, 'pytest', :h)"),
                  {"u": username, "h": auth.hash_password(password)})


@pytest.fixture(scope="module", autouse=True)
def signed_in():
    _remove_test_accounts()
    _add_account(USER, PASSWORD)
    r = client.post("/login", data={"username": USER, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in client.cookies
    yield
    _remove_test_accounts()


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


def test_response_needs_sign_in_then_records_and_is_undone():
    with settings.app_engine().connect() as c:
        aid = c.execute(text("SELECT action_id FROM brain.action WHERE status = 'open' LIMIT 1")).scalar()
    assert anon.post(f"/recommendations/{aid}/approve", follow_redirects=False).status_code == 303   # nothing recorded
    r = client.post(f"/recommendations/{aid}/approve")
    try:
        assert r.status_code == 200 and "APPROVED" in r.text.upper() and 'id="summary"' in r.text
    finally:
        with settings.etl_engine().begin() as c:                                 # undo
            c.execute(text("DELETE FROM brain.action_event WHERE by = 'pytest'"))
            c.execute(text("""UPDATE brain.action SET status = 'open', decided_at = NULL, decided_by = NULL,
                              decision_note = NULL WHERE action_id = :a"""), {"a": aid})


@pytest.mark.parametrize("path", ["/inventory", "/inventory?branch=KJ2+%2F+Branch+Dharampeth&old_days=180",
                                  "/sales", "/sales?start=2026-10-01&end=2026-10-31&group=Diamond+Jewellery",
                                  "/insights", "/insights?group=&group=D+Kundan+Jewellery", "/settings",
                                  "/stock-turn", "/stock-turn?scope=KJ2+%2F+Branch+Dharampeth&status=reduce&status=&search=ring",
                                  "/stock-turn?group=Diamond+Jewellery&group=", "/stock-turn?view=brick&search=D+RING",
                                  "/settings/ideal-turns", "/seasonality", "/seasonality?group=Gold", "/recommendations?sort=money", "/recommendations/feed?sort=newest&offset=25", "/lookup", "/lookup?q=D+RING", "/lookup?q=DRN21585", "/lookup?q=zzqqxx",
                                  "/lookup?q=Kothari", "/sales?salesperson=K0057&branch=&group=", "/settings/festivals",
                                  "/recommendations?group=festival", "/recommendations?group=purchasing"])
def test_analytics_and_settings_pages_render(path):
    r = client.get(path)
    assert r.status_code == 200, r.text[-800:]
    assert "Traceback" not in r.text and "NaT" not in r.text


def test_settings_save_rule_and_row_then_revert():
    me = client
    with settings.app_engine().connect() as c:
        aged = c.execute(text("SELECT value FROM config.setting WHERE key = 'aged_days'")).scalar()
        # a plain salesperson, and the whole row kept so every column is put back afterwards
        person = c.execute(text("SELECT * FROM config.salesperson WHERE kind = 'salesperson' "
                                "ORDER BY sales_code LIMIT 1")).one()
    targets = {"target_turn": "1.0", "overstock_days": "365", "understock_days": "60", "aged_days": "400",
               "max_offer_discount_pct": "15"}
    try:
        refused = anon.post("/settings/rules/Targets", data=targets, follow_redirects=False)  # not signed in: refused
        assert refused.status_code == 303 and refused.headers["location"].startswith("/login")
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
                    data={"name": person.name, "kind": person.kind, "active": "on", "note": "pytest note"})
        assert r.status_code == 200 and "pytest note" in r.text
    finally:
        with settings.etl_engine().begin() as c:
            c.execute(text("UPDATE config.setting SET value = CAST(:v AS jsonb), updated_by = 'default' WHERE key = 'aged_days'"),
                      {"v": str(aged)})
            row = person._asdict()
            c.execute(text("UPDATE config.salesperson SET " + ", ".join(f"{k} = :{k}" for k in row if k != "sales_code")
                           + " WHERE sales_code = :sales_code"), row)
            c.execute(text("DELETE FROM config.change_log WHERE changed_by = 'pytest'"))


# ── sign-in ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/recommendations", "/overview", "/settings", "/exports/transfers.xlsx",
                                  "/recommendations/12", "/account"])
def test_every_page_needs_sign_in(path):
    r = anon.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert anon.get("/login").status_code == 200 and anon.get("/static/app.css").status_code == 200


def test_htmx_request_without_session_is_sent_to_login():
    r = anon.get("/recommendations/feed", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_wrong_password_is_generic_and_locks_after_five():
    _add_account("pytest_lock", PASSWORD)
    wrong = anon.post("/login", data={"username": "pytest_lock", "password": "nope-nope-nope"})
    unknown = anon.post("/login", data={"username": "pytest_nobody", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert "username or password is incorrect" in wrong.text and "username or password is incorrect" in unknown.text
    for _ in range(4):
        anon.post("/login", data={"username": "pytest_lock", "password": "nope-nope-nope"})
    locked = anon.post("/login", data={"username": "pytest_lock", "password": PASSWORD}, follow_redirects=False)
    assert locked.status_code == 401 and "Too many wrong attempts" in locked.text      # right password, still locked
    with settings.app_engine().connect() as c:
        reasons = c.execute(text("SELECT reason FROM auth.login_event WHERE username = 'pytest_lock' "
                                 "ORDER BY event_id")).scalars().all()
    assert reasons == ["bad_password"] * 5 + ["locked"]


def test_sign_in_returns_to_the_page_asked_for_but_never_off_site():
    r = TestClient(app).post("/login", data={"username": USER, "password": PASSWORD, "next": "/sales?group=x"},
                             follow_redirects=False)
    assert r.headers["location"] == "/sales?group=x"
    for bad in ["https://evil.example", "//evil.example", "/\\evil.example"]:
        r = TestClient(app).post("/login", data={"username": USER, "password": PASSWORD, "next": bad},
                                 follow_redirects=False)
        assert r.headers["location"] == "/recommendations"


def test_cookie_flags_headers_and_sign_out():
    c = TestClient(app)
    r = c.post("/login", data={"username": USER, "password": PASSWORD}, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    page = c.get("/overview")
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
    assert "pytest" in page.text                                                     # display name in the top bar
    token = c.cookies[auth.COOKIE]
    c.post("/logout")
    assert auth.session_user(token) is None                                          # ended on the server too
    assert TestClient(app, cookies={auth.COOKIE: token}).get("/overview", follow_redirects=False).status_code == 303


def test_post_from_another_site_is_refused():
    r = client.post("/recommendations/1/comment", data={"note": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_change_password_signs_out_other_sessions():
    _add_account("pytest_pw", PASSWORD)
    a, b = TestClient(app), TestClient(app)
    for c in (a, b):
        c.post("/login", data={"username": "pytest_pw", "password": PASSWORD})
    assert "incorrect" in a.post("/account/password", data={"current": "wrong", "new": "x" * 12, "confirm": "x" * 12}).text
    assert "at least 8" in a.post("/account/password", data={"current": PASSWORD, "new": "short", "confirm": "short"}).text
    r = a.post("/account/password", data={"current": PASSWORD, "new": "a-new-password-1", "confirm": "a-new-password-1"})
    assert "Password changed" in r.text
    assert a.get("/overview", follow_redirects=False).status_code == 200            # this browser stays signed in
    assert b.get("/overview", follow_redirects=False).status_code == 303            # the other one is signed out
    assert TestClient(app).post("/login", data={"username": "pytest_pw", "password": "a-new-password-1"},
                                follow_redirects=False).status_code == 303


def test_users_tab_adds_resets_disables_and_forces_a_new_password():
    r = client.get("/settings/users")
    assert r.status_code == 200 and 'id="users"' in r.text and "you</span>" in r.text
    assert "already exists" in client.post("/settings/users", data={"username": USER, "display_name": "x",
                                                                    "password": "longenough1"}).text
    assert "at least 8" in client.post("/settings/users", data={"username": "pytest_new", "display_name": "New Person",
                                                                "password": "short"}).text
    r = client.post("/settings/users", data={"username": "pytest_new", "display_name": "New  Person", "password": "temp-pass-1"})
    assert "Added New Person" in r.text
    with settings.app_engine().connect() as c:
        uid = c.execute(text("SELECT user_id FROM auth.app_user WHERE username = 'pytest_new'")).scalar()

    new = TestClient(app)                                        # first sign-in with the temporary password
    assert new.post("/login", data={"username": "pytest_new", "password": "temp-pass-1"},
                    follow_redirects=False).status_code == 303
    assert new.get("/overview", follow_redirects=False).headers["location"] == "/account"
    assert new.get("/recommendations/feed", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/account"
    assert "temporary password" in new.get("/account").text
    assert "different" in new.post("/account/password", data={"current": "temp-pass-1", "new": "temp-pass-1",
                                                              "confirm": "temp-pass-1"}).text
    new.post("/account/password", data={"current": "temp-pass-1", "new": "my-own-pass-1", "confirm": "my-own-pass-1"})
    assert new.get("/overview", follow_redirects=False).status_code == 200    # free to use the app now

    r = client.post(f"/settings/users/{uid}/password", data={"password": "temp-pass-2"})
    assert "Temporary password set" in r.text
    assert new.get("/overview", follow_redirects=False).status_code == 303    # reset signed them out
    client.post(f"/settings/users/{uid}/disable")
    assert "account is switched off" in TestClient(app).post("/login", data={"username": "pytest_new",
                                                                             "password": "temp-pass-2"}).text
    client.post(f"/settings/users/{uid}/enable")
    assert TestClient(app).post("/login", data={"username": "pytest_new", "password": "temp-pass-2"},
                                follow_redirects=False).status_code == 303
    with settings.app_engine().connect() as c:
        me = c.execute(text("SELECT user_id FROM auth.app_user WHERE username = :u"), {"u": USER}).scalar()
        logged = c.execute(text("SELECT count(*) FROM config.change_log WHERE table_name = 'user' "
                                "AND row_key = 'pytest_new'")).scalar()
    assert "your own account" in client.post(f"/settings/users/{me}/disable").text   # can't lock yourself out
    assert logged == 4
    with settings.etl_engine().begin() as c:
        c.execute(text("DELETE FROM config.change_log WHERE table_name = 'user' AND row_key LIKE 'pytest%'"))


def test_ideal_turn_save_validate_and_remove():
    key = "D RING"
    try:
        assert "between 0.05 and 24" in client.post("/settings/ideal-turns", data={"level": "product", "key": key,
                                                                                    "ideal": "0"}).text
        r = client.post("/settings/ideal-turns", data={"level": "product", "key": key, "ideal": "0.8", "note": "pytest"})
        assert "ideal 0.8× a year" in r.text and "pytest" in r.text
        with settings.app_engine().connect() as c:
            assert c.execute(text("SELECT ideal FROM config.ideal_turn WHERE level = 'product' AND key = :k"),
                             {"k": key}).scalar() == Decimal("0.80")
        assert "Removed" in client.post("/settings/ideal-turns", data={"level": "product", "key": key, "delete": "1"}).text
    finally:
        with settings.etl_engine().begin() as c:
            c.execute(text("DELETE FROM config.ideal_turn WHERE note = 'pytest' OR (level = 'product' AND key = :k "
                           "AND updated_by = 'pytest')"), {"k": key})
            c.execute(text("DELETE FROM config.change_log WHERE table_name = 'ideal_turn' AND changed_by = 'pytest'"))


def test_data_quality_shows_the_audit_and_its_workbook():
    r = client.get("/checks")
    assert r.status_code == 200 and "Accuracy audit" in r.text
    with settings.app_engine().connect() as c:
        has_audit = c.execute(text("SELECT count(*) FROM brain.audit_run")).scalar() > 0
    if has_audit:
        x = client.get("/checks/audit.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"


def test_data_notice_appears_on_every_page_when_set():
    from kkj import notice
    with settings.app_engine().connect() as c:
        before = notice.get(c)
    try:
        with settings.app_engine().begin() as c:
            notice.set_notice(c, "pytest notice text", "pytest")
        assert "pytest notice text" in client.get("/overview").text
        with settings.app_engine().begin() as c:
            notice.clear(c, "pytest")
        assert "Data notice" not in client.get("/overview").text
    finally:
        with settings.app_engine().begin() as c:
            if before:
                notice.set_notice(c, before["text"], "restored")
            else:
                notice.clear(c, "restored")


def test_festival_date_save_and_remove():
    try:
        r = client.post("/settings/festivals", data={"name": "pytest fest", "day": "2027-01-15", "note": "pytest"})
        assert "Saved pytest fest" in r.text and "pytest fest" in r.text
        assert "Enter a festival name" in client.post("/settings/festivals", data={"name": "", "day": ""}).text
        r = client.post("/settings/festivals", data={"name": "pytest fest", "day": "2027-01-15", "delete": "1"})
        assert "Removed pytest fest" in r.text
    finally:
        with settings.etl_engine().begin() as c:
            c.execute(text("DELETE FROM config.festival WHERE name = 'pytest fest'"))
            c.execute(text("DELETE FROM config.change_log WHERE table_name = 'festival' AND changed_by = 'pytest'"))


def test_lookup_finds_a_piece_and_a_product():
    r = client.get("/lookup", params={"q": "DRN21585"})
    assert "Piece DRN21585" in r.text and "Where it has been" in r.text
    assert "Stock by branch" in client.get("/lookup", params={"q": "D RING"}).text
    assert "Nothing found" in client.get("/lookup", params={"q": "zzqqxx"}).text
    assert ">nan<" not in client.get("/recommendations").text


def test_stock_turn_shows_its_table_when_calculated():
    with settings.app_engine().connect() as c:
        calculated = c.execute(text("SELECT count(*) FROM brain.brick_turn")).scalar() > 0
    r = client.get("/stock-turn")
    if calculated:
        assert "Not calculated yet" not in r.text and "biggest changes first" in r.text


@pytest.mark.parametrize("path", ["/recommendations", "/overview", "/inventory", "/sales", "/stock-turn", "/seasonality",
                                  "/insights", "/checks", "/settings", "/lookup?q=D+TOPS"])
def test_no_missing_value_leaks_onto_pages(path):
    import re
    page = re.sub("<[^>]+>", " ", client.get(path).text)
    assert not re.search(r"(?<![A-Za-z])(nan|NaN|NaT|None)(?![A-Za-z])", page), path


def test_salespeople_scorecard_and_person_page():
    r = client.get("/salespeople?start=2026-04-01&show=all")
    assert r.status_code == 200 and "Traceback" not in r.text
    assert "Scorecard" in r.text and "vs colleagues" in r.text
    import re
    code = re.search(r'href="/salespeople/([^"?]+)\?', r.text).group(1)
    p = client.get(f"/salespeople/{code}?start=2026-04-01")
    assert p.status_code == 200 and "At the same branch" in p.text and "nan" not in p.text.lower().split("</script>")[-1]
    assert client.get("/salespeople/NOSUCHCODE").status_code == 404


def test_sets_on_inventory_and_sales():
    inv = client.get("/inventory")
    assert inv.status_code == 200 and "Sets in stock" in inv.text and "Broken sets" in inv.text
    sales = client.get("/sales?start=2026-04-01")
    assert sales.status_code == 200 and "Sold whole or split" in sales.text
    assert client.get("/recommendations?group=sets").status_code == 200


def test_stale_approvals_bills_to_check_and_outcomes():
    inv = client.get("/inventory?approval_days=90")
    assert inv.status_code == 200 and ("Stale approvals" in inv.text or "No piece has been out on approval" in inv.text)
    chk = client.get("/checks")
    assert chk.status_code == 200 and "Bills to check" in chk.text
    out = client.get("/recommendations/outcomes")
    assert out.status_code == 200 and "Outcomes" in out.text
