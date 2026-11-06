"""
What the app covers: diamonds only for now. Gold, silver and platinum are kept in sales.hist_line (the five-year
history import takes every group) for when the app widens, but every read filters to these groups.
"""
# the item groups the ERP diamond product groups (5, 100, 126, 14, 121, 127, 69, 18) return
DIAMOND_GROUPS = ["Diamond Jewellery", "D Kundan Jewellery", "LOOSE DIAMOND", "MRP DIAMOND",
                  "Broken Dia Jew", "Customer Diamond"]
