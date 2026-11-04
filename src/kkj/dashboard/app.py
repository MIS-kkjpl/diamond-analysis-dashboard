r"""
KKJ Diamond Brain (Streamlit). Reads as powerbi_reader; decisions and settings are saved
as dashboard_app, which can write only the brain and config schemas.

Run from the project folder:
  .venv\Scripts\streamlit run src\kkj\dashboard\app.py
"""
import streamlit as st

from kkj.dashboard.common import apply_theme
from kkj.dashboard.pages import checks, decisions, insights_page, overview, sales, settings_page, stock

st.set_page_config(page_title="KKJ Diamond Brain", page_icon="💎", layout="wide")
apply_theme()

with st.sidebar:
    st.markdown("### 💎 KKJ Diamond Brain")
    st.caption("Live stock, sales and decisions for diamond products")
    st.text_input("Your name", key="who", placeholder="Recorded with your decisions",
                  help="Approvals, rejections, notes and settings changes are recorded with this name.")

pages = {
    "Decide": [st.Page(decisions.render, title="Decisions", icon=":material/bolt:", url_path="decisions", default=True)],
    "Monitor": [st.Page(overview.render, title="Overview", icon=":material/monitoring:", url_path="overview"),
                st.Page(stock.render, title="Stock health", icon=":material/inventory_2:", url_path="stock"),
                st.Page(sales.render, title="Sales", icon=":material/point_of_sale:", url_path="sales")],
    "Learn": [st.Page(insights_page.render, title="Insights", icon=":material/insights:", url_path="insights")],
    "System": [st.Page(checks.render, title="Data checks", icon=":material/fact_check:", url_path="checks"),
               st.Page(settings_page.render, title="Settings", icon=":material/tune:", url_path="settings")],
}
st.navigation(pages).run()
