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
    # ── scope (kkj.scope): what never counts as stock or sales ──
    Rule("excluded_groups", "Scope", "Leave out these item groups", ["Customer Diamond"], "",
         "Never counted as stock or sales anywhere: customers' own items (repairs, exchanges), or any group that is "
         "not the company's merchandise. They stay in the data loads. Recommendations follow on the next refresh.",
         "list", choices=("Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND", "Broken Dia Jew",
                          "Customer Diamond")),
    Rule("excluded_products", "Scope", "Leave out these products", [], "",
         "Products (item names) that should not count, e.g. a niche or non-merchandise line. Same effect as above.",
         "list"),
    # ── targets ──
    Rule("overstock_days", "Targets", "Overstocked above", 365, "days of stock", "A group of similar pieces at a "
         "branch holding more than this many days of sales at its current rate is overstocked.", "int", min=30, max=1460),
    Rule("understock_days", "Targets", "Understocked below", 60, "days of stock", "A selling group of similar pieces "
         "with fewer days of stock than this at a branch is about to run short.", "int", min=7, max=365),
    Rule("aged_days", "Targets", "A piece is aged after", 365, "days since labelling",
         "Aged pieces become candidates for an offer, a transfer or clearance.", "int", min=90, max=1460),
    Rule("max_offer_discount_pct", "Targets", "Largest discount the system may suggest", 15.0, "%",
         "Offer suggestions never go above this. Today the top 5% of discounts are 14.5% and above.", min=0, max=60),
    Rule("markdown_age_steps", "Targets", "Markdown age steps", [365, 730], "days since labelling",
         "Where the suggested markdown steps up. With 365, 730 and percentages 5, 10, 15: up to a year 5%, one to two "
         "years 10%, over two years 15%. Only pieces older than 'A piece is aged after' get a markdown at all.", "list"),
    Rule("markdown_pct_steps", "Targets", "Markdown percentages", [5, 10, 15], "%",
         "The markdown for each age step, youngest first: one more than there are age steps. Never above the "
         "largest discount above.", "list"),
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
    Rule("target_turn", "Stock turn", "Reference stock turn", 1.0, "× a year",
         "The turn the Stock turn page compares each brick with. Analysis only: Increase / Reduce recommendations "
         "follow the ideal stock (Ideal stock section). The company turns about 0.5x today.", min=0.1, max=12),
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
    # ── demand forecast (kkj.metrics.forecast) ──
    Rule("fc_exclude_orders", "Demand forecast", "Leave customer-order sales out of demand", True, "",
         "Sales of pieces booked against a customer order are custom work, not demand the stock on the counter has "
         "to meet. When on, the forecast and ideal stock ignore them (they stay in every sales figure).", "bool"),
    Rule("fc_level_months", "Demand forecast", "Recent selling rate measured over the last", 12, "months",
         "Longer = steadier, slower to react to a change.", "int", min=3, max=36),
    Rule("fc_half_life_months", "Demand forecast", "Recent months count more: a month's weight halves every", 6,
         "months", "6 = a month half a year ago counts half as much as last month. Higher = all months count "
         "more evenly.", min=1, max=60),
    Rule("fc_stockout_correction", "Demand forecast", "Correct for months a group was out of stock", False, "",
         "A group that sold 2 while in stock half the month was selling at a rate of 4. Off by default: with about "
         "two stock readings a month, pieces that arrive and sell between readings make groups look out of stock, "
         "and the accuracy test (Nov 2026) showed the correction overstating demand by 10-40%. Worth trying again "
         "once a year of daily stock is in.", "bool"),
    Rule("fc_availability_basis", "Demand forecast", "Out of stock means out of stock", "company", "",
         "company = nowhere in the company (branches sell pieces brought in from Head Office or another branch, so a "
         "group missing from one counter was still on offer); branch = not at that branch.", "choice",
         choices=("company", "branch")),
    Rule("fc_min_availability_pct", "Demand forecast", "Ignore months in stock less than", 25.0, "% of the month",
         "Such months (if nothing sold) say nothing about demand. Availability is never taken below this either, "
         "so one lucky sale cannot blow up the rate.", min=5, max=100),
    Rule("fc_shrink_months", "Demand forecast", "A group relies on its own history after about", 6,
         "months of selling data", "Before that it leans on its parent (product, then item group) at the branch: "
         "with this many months observed, own history and parent count equally. Higher = more borrowing, steadier "
         "forecasts.", min=0.5, max=60),
    Rule("fc_seasonal", "Demand forecast", "Follow the seasonal pattern", False, "",
         "Forecast each month with the item group's (or product's) monthly pattern from five years of sales. Off by "
         "default: Diwali moves between October and November, so a calendar-month pattern made the 2-month forecast "
         "less accurate in the accuracy test (Nov 2026). Festival stock is planned by the festival recommendations "
         "instead.", "bool"),
    Rule("fc_season_level", "Demand forecast", "Seasonal pattern taken from", "item_group", "",
         "item_group = the item group's pattern for all its products (steadier); product = each product's own "
         "pattern where it sells enough (Seasonality section), else its item group's.", "choice",
         choices=("item_group", "product")),
    Rule("fc_trend", "Demand forecast", "Follow the trend", True, "",
         "Scale the forecast by the change in a product's sales at the branch over the last year against the year "
         "before (steadied by its item group's trend).", "bool"),
    Rule("fc_trend_cap_pct", "Demand forecast", "Limit the trend to", 30.0, "% up or down",
         "A product growing fast is not assumed to keep growing without limit.", min=0, max=200),
    Rule("fc_trend_pseudo_pcs", "Demand forecast", "Steady a product's trend with its item group's over", 20,
         "pieces", "Higher = products follow their item group's trend more closely.", min=0, max=500),
    Rule("fc_backtest_months", "Demand forecast", "Accuracy test over the last", 24, "months",
         "The forecast is replayed as of each past month-end in this period, using only what was known then, and "
         "compared with what actually sold and with two simple methods.", "int", min=6, max=48),
    # ── ideal stock (kkj.metrics.ideal_stock) ──
    Rule("replacement_days", "Ideal stock", "Replacement time", 45, "days",
         "From deciding to restock until the new piece is on the counter. Stock must cover demand for this long. "
         "Set it per product or item group under Settings → Ideal stock by group.", "int", min=1, max=365),
    Rule("review_days", "Ideal stock", "Stock is reviewed every", 7, "days",
         "Recommendations refresh this often, so stock also has to last until the next review.", "int", min=1, max=90),
    Rule("service_a_pct", "Ideal stock", "Grade A groups: have enough stock", 95.0, "% of the time",
         "The chance of meeting demand until replacement stock arrives. Higher = fewer lost sales, more stock.",
         min=50, max=99.9),
    Rule("service_b_pct", "Ideal stock", "Grade B groups: have enough stock", 90.0, "% of the time", "", min=50, max=99.9),
    Rule("service_c_pct", "Ideal stock", "Grade C groups: have enough stock", 80.0, "% of the time", "", min=50, max=99.9),
    Rule("grade_a_pct", "Ideal stock", "Grade A = the top", 20.0, "% of groups by sales value",
         "Groups ranked by the last 12 months' sales value. Override a group's grade under Settings → Ideal stock "
         "by group.", min=0, max=100),
    Rule("grade_b_pct", "Ideal stock", "Grade B = the next", 30.0, "% of groups by sales value",
         "Everything below is grade C, including groups with no sales.", min=0, max=100),
    Rule("display_min_pcs", "Ideal stock", "Display minimum", 1, "pieces",
         "The least a branch holds of a group it sells, for choice on the counter, whatever the forecast says. "
         "Set it per product or group under Settings → Ideal stock by group.", "int", min=0, max=50),
    Rule("display_min_months", "Ideal stock", "Display minimum applies to groups the branch sold in the last", 12,
         "months", "Groups a branch has not sold for longer get no minimum: their ideal comes from the forecast "
         "alone.", "int", min=1, max=60),
    Rule("choice_months", "Ideal stock", "Hold for choice at least", 18, "months of forecast demand",
         "Jewellery is not interchangeable: customers choose among designs, and branch-months holding more of a group "
         "sold more of it (about +3% sales for +10% pieces, Nov 2026 analysis). So a branch holds at least this many "
         "months of its forecast sales, whatever the replacement time. 12 = an implied turn of about 1x, 18 about "
         "0.67x (chosen Nov 2026: leaner without a shock). Set it per "
         "product or group under Settings → Ideal stock by group (e.g. longer for bridal sets).", min=0, max=60),
    Rule("ideal_band_pct", "Ideal stock", "Leave stock as it is when within", 20.0, "% of the ideal",
         "Small differences are not worth acting on. A change also has to be at least the smallest change worth "
         "recommending (Stock turn section).", min=0, max=100),
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
    Rule("markdown_hold_days", "Seasonality", "Hold markdowns before a festival", 21, "days before the festival",
         "From this many days before a festival (Settings > Festivals) until festival buying ends, no new markdowns are "
         "suggested: aged pieces often sell at full price then. Markdowns already raised stay, marked as on hold and "
         "lower in the list. 0 = no hold.", "int", min=0, max=60),
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
