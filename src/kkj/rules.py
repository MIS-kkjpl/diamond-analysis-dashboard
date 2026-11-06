"""
Business rules: the settings the recommendations run on, editable from the dashboard.

Each rule is defined here once (label, unit, default, explanation); its current value
lives in config.setting. Defaults are STARTING POINTS to be tuned by the business,
not facts. Every change is recorded in config.change_log.
"""
import json
from dataclasses import dataclass, field

from sqlalchemy import text


@dataclass(frozen=True)
class Rule:
    key: str
    section: str
    label: str
    default: object
    unit: str = ""
    help: str = ""
    kind: str = "number"            # number | int | bool | list | choice
    choices: tuple = field(default_factory=tuple)
    min: float | None = None
    max: float | None = None


RULES = [
    # ── targets ──
    Rule("overstock_days", "Targets", "Overstocked above", 365, "days of stock", "A group of similar pieces at a "
         "branch holding more than this many days of sales at its current rate is overstocked.", "int", min=30, max=1460),
    Rule("understock_days", "Targets", "Understocked below", 60, "days of stock", "A selling group of similar pieces "
         "with fewer days of stock than this at a branch is about to run short.", "int", min=7, max=365),
    Rule("aged_days", "Targets", "A piece is aged after", 365, "days since labelling",
         "Aged pieces become candidates for an offer, a transfer or clearance.", "int", min=90, max=1460),
    Rule("max_offer_discount_pct", "Targets", "Largest discount the system may suggest", 15.0, "%",
         "Offer suggestions never go above this. Today the top 5% of discounts are 14.5% and above.", min=0, max=60),
    # ── how demand is measured ──
    Rule("demand_window_days", "Demand", "Selling rate measured over the last", 180, "days",
         "The window for each group's selling rate. Longer = steadier, slower to react.", "int", min=30, max=730),
    Rule("min_sold_to_count_as_selling", "Demand", "A group counts as selling after", 2, "pieces sold in the window",
         "Below this, a group's selling rate is treated as too thin to act on.", "int", min=1, max=20),
    Rule("cluster_dimensions", "Demand", "Group similar pieces by", ["item_name", "price_band", "ct_band"], "",
         "Pieces are compared within groups of similar pieces; demand is measured per group and branch.", "list",
         choices=("item_class_group", "item_name", "category", "price_band", "ct_band", "karat", "supplier")),
    Rule("price_band_edges", "Demand", "Price band edges", [50_000, 100_000, 200_000, 300_000, 500_000, 1_000_000],
         "₹", "Taxable value boundaries between price bands.", "list"),
    Rule("ct_band_edges", "Demand", "Diamond weight band edges", [0.1, 0.25, 0.5, 1.0, 2.0], "ct",
         "Diamond weight boundaries between carat bands.", "list"),
    Rule("no_buy_groups", "Demand", "Never suggest buying", ["Broken Dia Jew", "Customer Diamond"], "",
         "Item groups that don't come from suppliers (customers' broken or own pieces): no reorder or "
         "pause-buying suggestions for them.", "list",
         choices=("Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND", "Broken Dia Jew",
                  "Customer Diamond")),
    # ── weighted stock turn per brick (kkj.metrics.brick_turn) ──
    Rule("target_turn", "Stock turn", "Default ideal stock turn", 1.0, "× a year",
         "How many times a year stock should sell through, for every brick without its own ideal (set ideals per "
         "brick, group, product or item group under Settings → Ideal turns). The company turns about 0.6× today.",
         min=0.1, max=12),
    Rule("turn_band_pct", "Stock turn", "Keep stock as it is when the turn is within", 10.0, "% of the ideal",
         "Inside this band a brick is left alone; below it is a candidate to reduce, above it to increase.",
         min=0, max=50),
    Rule("turn_availability_pct", "Stock turn", "A window counts when the brick was in stock in at least", 60.0,
         "% of its fortnights", "Turn is only trusted over a window where the brick was actually available.",
         min=10, max=100),
    Rule("turn_history_pct", "Stock turn", "A window needs stock data for at least", 60.0, "% of its fortnights",
         "Fortnights without a usable stock snapshot (missing or set aside by the data checks) are not counted "
         "against a brick; below this share the window is not judged at all.", min=10, max=100),
    Rule("turn_w3_all", "Stock turn", "All three windows qualify: weight of the last 3 months", 60.0, "%",
         "The three weights are scaled to add up to 100.", min=0, max=100),
    Rule("turn_w6_all", "Stock turn", "All three windows qualify: weight of the last 6 months", 30.0, "%", "", min=0, max=100),
    Rule("turn_w12_all", "Stock turn", "All three windows qualify: weight of the last 12 months", 10.0, "%", "", min=0, max=100),
    Rule("turn_w6_of_6_12", "Stock turn", "Only 6 and 12 months qualify: weight of 6 months", 75.0, "%",
         "12 months gets the rest.", min=0, max=100),
    Rule("turn_w3_of_3_6", "Stock turn", "Only 3 and 6 months qualify (new product): weight of 3 months", 75.0, "%",
         "6 months gets the rest.", min=0, max=100),
    Rule("turn_brick_levels", "Stock turn", "A brick is made of", ["purity", "item_name", "category", "sub_category",
                                                                   "weight_band"], "",
         "Purity – Product (item) – Variety (category) – Type (sub-category) – Weight range.", "list",
         choices=("purity", "item_name", "category", "sub_category", "weight_band")),
    Rule("turn_group_levels", "Stock turn", "A brick group (where recommendations are made) is made of",
         ["purity", "item_name", "weight_band"], "",
         "Bricks are tracked individually; recommendations are made per group of bricks, from the group's pooled "
         "stock and sales, with its bricks listed as evidence.", "list",
         choices=("purity", "item_name", "category", "sub_category", "weight_band")),
    Rule("turn_weight_basis", "Stock turn", "Weight range measured by", "diamond_ct", "",
         "diamond_ct = total diamond weight (the usual measure for diamond jewellery); gross_wt = gross weight in "
         "grams (gold jewellery). Change the edges below with it.", "choice", choices=("diamond_ct", "gross_wt")),
    Rule("turn_weight_edges", "Stock turn", "Weight range edges", [0.1, 0.25, 0.5, 1.0, 2.0, 5.0], "ct (or g)",
         "Boundaries between weight ranges, smallest first.", "list"),
    Rule("turn_hold_months", "Stock turn", "Hold a thin brick with healthy neighbours for", 3, "months",
         "A brick with too little availability to judge, whose neighbouring weight ranges turn at the ideal "
         "rate, is kept this long before it is judged again.", "int", min=1, max=12),
    Rule("turn_min_change_pcs", "Stock turn", "Smallest change worth recommending", 2, "pieces",
         "Increase / reduce recommendations are raised only when the ideal stock differs by at least this.",
         "int", min=1, max=50),
    # ── seasonality (kkj.metrics.seasonality) ──
    Rule("turn_seasonal_adjust", "Seasonality", "Adjust stock turn for the season", True, "",
         "A 3-month window over Diwali holds far more than a quarter of the year's sales. When on, each window's "
         "sales are annualised by the share of a typical year it covers (from five years of sales), not by its "
         "length.", "bool"),
    Rule("season_min_year_pcs", "Seasonality", "A product has its own seasonal pattern after", 100,
         "pieces sold a year", "Below this a product uses its item group's pattern.", "int", min=10, max=5000),
    Rule("festival_days_before", "Seasonality", "Festival buying starts", 21, "days before the festival",
         "The window around each festival (Settings → Festivals) that its sales are measured over.", "int",
         min=3, max=60),
    Rule("festival_days_after", "Seasonality", "Festival buying ends", 3, "days after the festival", "", "int",
         min=0, max=15),
    Rule("festival_lead_days", "Seasonality", "Prepare for a festival from", 90, "days ahead",
         "Festival stock recommendations appear this far ahead (and stop 7 days before, when it is too late "
         "to buy).", "int", min=14, max=180),
    Rule("festival_stock_cover", "Seasonality", "Before a festival, hold at least", 2.0,
         "× its expected festival sales", "Customers need choice: a product expected to sell 50 pieces over "
         "the festival should have at least this many times that on hand.", min=1, max=6),
    # ── sets (kkj.metrics.sets) ──
    Rule("broken_set_min_value", "Sets", "Recommend action on a broken set worth at least", 25_000, "₹",
         "A set that has lost a member (sold separately or moved out), or whose pieces sit in different places, "
         "gets a recommendation when what is left is worth at least this.", "int", min=0, max=10_000_000),
    # ── transfers ──
    Rule("transfer_enabled", "Transfers", "Suggest transfers between branches", True, "",
         "Move pieces from a branch with excess to one short of the same group.", "bool"),
    Rule("transfer_fill_to_days", "Transfers", "Fill the receiving branch up to", 180, "days of stock",
         "Transfers stop once the receiving branch would hold this many days of stock.", "int", min=14, max=730),
    Rule("transfer_min_value", "Transfers", "Smallest transfer worth suggesting", 25_000, "₹",
         "Transfers below this value are not suggested.", "int", min=0),
    Rule("transfer_excluded_branches", "Transfers", "Never transfer to or from", [], "",
         "Branches left out of transfer suggestions (e.g. wholesale, exhibitions).", "list"),
    Rule("transfer_piece_priority", "Transfers", "Which pieces move first", "oldest", "",
         "oldest = the longest-held pieces at the sending branch go first.", "choice", choices=("oldest", "lowest_value", "highest_value")),
    # ── discounts ──
    Rule("fast_group_days", "Discounts", "A group is fast-selling below", 90, "days of stock",
         "Discount given on fast-selling groups is counted as avoidable.", "int", min=7, max=365),
    # ── insights ──
    Rule("insight_months", "Insights", "Insights look back over", 12, "months",
         "The period the insight analyses use (sell rate by age, suppliers, assortment).", "int", min=3, max=60),
    Rule("min_supplier_pcs", "Insights", "Score suppliers holding at least", 40, "pieces on average",
         "Suppliers with less stock than this are not scored (too few pieces to judge).", "int", min=5, max=500),
    Rule("liquidation_age_days", "Insights", "A piece is a liquidation candidate after", 1095, "days since labelling",
         "Past this age pieces almost never sell (the sell-rate-by-age curve shows where).", "int", min=365, max=3650),
    Rule("diamond_recovery_pct", "Insights", "Share of diamond value recovered if a piece is broken up", 70.0, "%",
         "Used for the recovery value of liquidation candidates: metal at the piece's rate + this share of its "
         "diamond value. An estimate to tune, not a valuation.", min=0, max=100),
    # ── money ──
    Rule("capital_cost_pct", "Money", "Annual cost of money tied up in stock", 12.0, "%",
         "Used to show what slow and aged stock costs to hold, in ₹ a month.", min=0, max=40),
]
BY_KEY = {r.key: r for r in RULES}


def load(conn):
    """{key: value} for every rule: stored value, else the default."""
    stored = dict(conn.execute(text("SELECT key, value FROM config.setting")).all())
    return {r.key: stored.get(r.key, r.default) for r in RULES}


def seed(conn):
    """Insert defaults for rules not stored yet (never overwrites); returns how many were added."""
    added = 0
    for r in RULES:
        added += conn.execute(text("""
            INSERT INTO config.setting (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), 'default')
            ON CONFLICT (key) DO NOTHING"""), {"k": r.key, "v": json.dumps(r.default)}).rowcount
    return added


def save(conn, key, value, changed_by, note=None):
    """Store one rule's new value and record the change. Caller commits."""
    if key not in BY_KEY:
        raise KeyError(key)
    old = conn.execute(text("SELECT value FROM config.setting WHERE key = :k"), {"k": key}).scalar()
    if old == value:
        return False
    conn.execute(text("""
        INSERT INTO config.setting (key, value, updated_at, updated_by)
        VALUES (:k, CAST(:v AS jsonb), now(), :by)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by"""),
        {"k": key, "v": json.dumps(value), "by": changed_by})
    conn.execute(text("""
        INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value, note)
        VALUES (:by, 'setting', :k, CAST(:old AS jsonb), CAST(:new AS jsonb), :note)"""),
        {"by": changed_by, "k": key, "old": json.dumps(old), "new": json.dumps(value), "note": note})
    return True
