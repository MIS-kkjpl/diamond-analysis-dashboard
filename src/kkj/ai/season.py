"""
Season notes: what the coming festivals and months mean for stock, in plain language, from the figures the
Seasonality page computes (monthly pattern by item group, festival lift, the festival outlook by product).
"""
import json

import pandas as pd
from sqlalchemy import text

from kkj import rules as rules_mod
from kkj import settings
from kkj.ai import client as ai
from kkj.ai import notes
from kkj.brain.run import festival_outlook
from kkj.db import queries as q
from kkj.metrics import seasonality as sn

SYSTEM = """You write short planning notes on the season ahead for a diamond jewellery retailer in India, for the
people who decide what to buy, move and promote.

Use only the figures given. Do not invent numbers or festival dates. Format (Markdown, under 300 words):
## The next 90 days         (2-3 bullets: which months and festivals lift which item groups, by how much)
## Prepare                  (products short of their festival target, and by how many pieces, if any)
## Hold back                (where stock already covers the season, or the season is quiet)
## Caveats                  (one line: the pattern is from past years' pieces sold; this year can differ)
An index of 1.0 is an average month; a lift of 1.8 means 1.8x a normal day's sales around that festival."""


def facts():
    with settings.app_engine().connect() as conn:
        rules = rules_mod.load(conn)
        daily = q.hist_daily(conn)
        fests = q.festivals(conn)
        stock = q.stock_latest(conn)
        if daily.empty:
            raise ai.AIError("The five-year sales history isn't loaded, so there is no seasonal pattern to describe.")
        today = pd.Timestamp.today().normalize()
        outlook = festival_outlook(conn, rules, today, stock.assign(value=stock["taxable_amount"].fillna(stock["item_amt"])))
    idx = sn.monthly_index(daily, "item_class_group", rules["season_min_year_pcs"])
    lift = sn.festival_lift(daily, fests, "item_class_group", rules["festival_days_before"], rules["festival_days_after"],
                            rules["season_min_year_pcs"])
    ahead = fests[(fests["day"] >= today) & (fests["day"] <= today + pd.Timedelta(days=120))]
    if len(outlook):
        outlook = outlook.assign(target=(outlook["expected_pcs"] * rules["festival_stock_cover"]).round(),
                                 short_by=lambda d: (d["target"] - d["held_pcs"]).clip(lower=0))
    return {"today": today.date().isoformat(), "festival_stock_cover": rules["festival_stock_cover"],
            "festivals_next_120_days": [{"name": f.name, "day": f.day.date().isoformat()} for f in ahead.itertuples()],
            "monthly_index_by_item_group": json.loads(idx.round(2).to_json(orient="records")),
            "festival_lift_by_item_group": json.loads(lift.round(2).to_json(orient="records")),
            "festival_outlook_by_product": json.loads(outlook.round(1).to_json(orient="records", date_format="iso"))
            if len(outlook) else []}


def write(who="system"):
    f = facts()
    msg, call_id = ai.call("ai_season", who, max_tokens=6000, system=notes.system(SYSTEM), output_config={"effort": "medium"},
                           messages=[{"role": "user", "content": "Figures:\n" + ai.as_json(f)}])
    with settings.app_engine().begin() as conn:
        return conn.execute(text("""INSERT INTO ai.text (kind, created_by, period, body_md, facts, call_id)
                                    VALUES ('season', :w, :p, :b, CAST(:f AS jsonb), :c) RETURNING text_id"""),
                            {"w": who, "p": f"from {pd.Timestamp(f['today']):%d %b %Y}", "b": ai.text_of(msg),
                             "f": json.dumps(f, default=str), "c": call_id}).scalar_one()
