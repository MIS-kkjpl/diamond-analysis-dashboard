"""Outcomes against real recommendations, inside a transaction that is always rolled back (nothing is decided)."""
import pytest
from sqlalchemy import text

from kkj import settings
from kkj.brain import outcomes


@pytest.fixture
def conn():
    c = settings.app_engine().connect()
    tx = c.begin()
    yield c
    tx.rollback()
    c.close()


def _decide(conn, kind, status, days_ago=20):
    aid = conn.execute(text("SELECT action_id FROM brain.action WHERE kind = :k ORDER BY action_id LIMIT 1"),
                       {"k": kind}).scalar()
    if aid is None:
        pytest.skip(f"no {kind} recommendation stored")
    conn.execute(text("""UPDATE brain.action SET status = :s, decided_at = now() - make_interval(days => :d), decided_by = 'pytest'
                         WHERE action_id = :a"""), {"s": status, "d": days_ago, "a": aid})
    return aid


def test_piece_recommendation_is_followed(conn):
    aid = _decide(conn, "offer", "approved")
    f = outcomes.follow(conn, [aid])
    assert len(f) == 1 and f.iloc[0]["days"] >= 19
    p = outcomes.pieces(conn, f)
    assert len(p) == f.iloc[0]["n_pieces"] > 0
    assert set(p["outcome"]) <= {"sold", "moved", "unchanged", "left stock, no sale", "delabelled"}
    assert "of" in f.iloc[0]["outcome"]


def test_purchasing_recommendation_compares_stock_then_and_now(conn):
    aid = _decide(conn, "turn_reduce", "rejected")
    f = outcomes.follow(conn, [aid])
    assert f.iloc[0]["outcome"].startswith("stock ")
    t = outcomes.by_kind(f)
    assert t.iloc[0]["decision"] == "dismissed"
