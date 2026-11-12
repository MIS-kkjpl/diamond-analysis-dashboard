import pandas as pd
from sqlalchemy import text

from kkj import settings
from kkj.db import queries as q
from kkj.scope import excluded, own_goods


def _excluded():
    with settings.app_engine().connect() as c:
        return excluded(c)


def test_customer_diamond_is_excluded_by_default_setting():
    groups, products = _excluded()
    assert "Customer Diamond" in groups


def test_company_queries_hold_no_excluded_items():
    groups, products = _excluded()
    with settings.app_engine().connect() as c:
        frames = [q.stock_latest(c), q.sales_lines(c, pd.Timestamp("2026-01-01").date()), q.hist_daily(c),
                  q.brick_sales(c, pd.Timestamp("2026-06-01").date())]
    for df in frames:
        assert not df["item_class_group"].isin(groups).any()
        assert not df["item_name"].isin(products).any()


def test_own_goods_condition_takes_aliases_and_lists():
    with settings.app_engine().connect() as c:
        n = c.execute(text(f"""SELECT count(*) FROM (VALUES ('Customer Diamond', 'X'), ('Diamond Jewellery', 'D RING'),
                                   ('Diamond Jewellery', 'NICHE'), (NULL, NULL)) t(item_class_group, item_name)
                               WHERE {own_goods('t')}"""),
                      {"x_groups": ["Customer Diamond"], "x_products": ["NICHE"]}).scalar()
    assert n == 2                                   # D RING and the row with no group or name


def test_out_of_stock_locations_are_not_stock_but_their_sales_count():
    from kkj.scope import excluded_locations
    with settings.app_engine().connect() as c:
        out = excluded_locations(c)
        assert "HO MD COUNTER" in out                       # delabelled pieces (Stock Outward, MHODDJ)
        latest = q.stock_latest(c)
        hist = q.stock_history(c, pd.Timestamp("2026-10-01").date())
        dates = q.stock_dates(c)["snapshot_date"].tail(2)
        bricks = q.brick_stock(c, list(dates))
    for df in (latest, hist, bricks):
        assert not df["location"].isin(out).any()
