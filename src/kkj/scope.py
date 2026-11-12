"""
What the app covers.

1. Diamonds only, for now. Gold, silver and platinum are kept in sales.hist_line (the five-year history import takes
   every group) for when the app widens, but every read filters to DIAMOND_GROUPS.
2. Exclusions: item groups and products that never count as stock or sales, set in Settings -> Scope (rules
   excluded_groups / excluded_products, with history). Customers' own items ('Customer Diamond': repairs,
   exchanges) are the standard case; any niche product can be added the same way. They stay in the raw tables
   (the loads mirror the ERP exactly); every company query filters them with own_goods().
3. Out-of-stock locations (rule excluded_locations): pieces at HO MD COUNTER leave by Stock Outward: damaged
   piece or tag (doc series MHODDJ) or the remaining piece of a broken set (MHODB), relabelled later under a new
   barcode. They are not stock. Stock queries add in_stock().
"""
# the item groups the ERP diamond product groups (5, 100, 126, 14, 121, 127, 69, 18) return
DIAMOND_GROUPS = ["Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND",
                  "Broken Dia Jew", "Customer Diamond"]


def own_goods(alias=""):
    """SQL condition keeping only rows that count (not an excluded item group or product). Needs the parameters
    :x_groups and :x_products, which kkj.db.queries.read() adds from the current settings."""
    a = f"{alias}." if alias else ""
    return (f"NOT (coalesce({a}item_class_group, '') = ANY(:x_groups) "
            f"OR coalesce({a}item_name, '') = ANY(:x_products))")


def in_stock(alias=""):
    """SQL condition for stock rows: not at an out-of-stock location. Needs :x_locations (added by
    kkj.db.queries.read())."""
    a = f"{alias}." if alias else ""
    return f"NOT coalesce({a}location, '') = ANY(:x_locations)"


def excluded_locations(conn):
    from kkj import rules
    return list(rules.load(conn)["excluded_locations"])


def excluded(conn):
    """(item groups, products) excluded in the current settings."""
    from kkj import rules
    r = rules.load(conn)
    return list(r["excluded_groups"]), list(r["excluded_products"])
