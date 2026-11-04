r"""
KKJ diamond dashboard (Streamlit). Read-only: connects as powerbi_reader.

Run from the project folder:
  .venv\Scripts\streamlit run src\kkj\dashboard\app.py
"""
import streamlit as st

from kkj.dashboard.pages import checks, overview, sales, stock

st.set_page_config(page_title="KKJ Diamonds", page_icon="💎", layout="wide")

pages = [
    st.Page(overview.render, title="Overview", url_path="overview", default=True),
    st.Page(stock.render, title="Stock health", url_path="stock"),
    st.Page(sales.render, title="Sales", url_path="sales"),
    st.Page(checks.render, title="Data checks", url_path="checks"),
]
st.navigation(pages).run()
