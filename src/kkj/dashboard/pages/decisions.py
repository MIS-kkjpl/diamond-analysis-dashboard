"""Decisions: the live feed of what to do, ranked by money, with approve / reject / snooze / done."""
import html
import json

import pandas as pd
import streamlit as st
from sqlalchemy import text

from kkj import settings
from kkj.brain import store
from kkj.brain.generators import money
from kkj.dashboard.common import pill

KINDS = {  # kind -> (feed label, icon)
    "transfer": ("Transfer", ":material/swap_horiz:"),
    "understock": ("Running short", ":material/production_quantity_limits:"),
    "offer": ("Offer", ":material/sell:"),
    "liquidate": ("Liquidate", ":material/recycling:"),
    "supplier_slow": ("Supplier", ":material/factory:"),
    "supplier_fast": ("Supplier", ":material/factory:"),
    "pause_buying": ("Buying", ":material/shopping_cart_off:"),
    "buy_band": ("Buying", ":material/shopping_cart:"),
    "discount": ("Discount", ":material/percent:"),
}
GROUPS = {"All": None, "Transfers": ["transfer"], "Running short": ["understock"], "Offers": ["offer"],
          "Liquidate": ["liquidate"], "Suppliers": ["supplier_slow", "supplier_fast"],
          "Buying": ["pause_buying", "buy_band"], "Discounts": ["discount"]}
VIEWS = {"To decide": ["open"], "Approved": ["approved"], "Snoozed": ["snoozed"], "Rejected": ["rejected"],
         "Closed": ["done", "resolved"]}
CONFIDENCE = {"high": ":material/signal_cellular_alt: high confidence",
              "medium": ":material/signal_cellular_alt_2_bar: medium confidence",
              "low": ":material/signal_cellular_alt_1_bar: low confidence"}
PAGE = 20


@st.cache_resource
def _engine():
    return settings.app_engine()


def _read(sql, **params):
    with _engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def _decide(action_id, decision, who, note=None):
    with _engine().begin() as conn:
        store.decide(conn, action_id, decision, who, note)
    st.toast(f"Recorded: {decision} by {who}")


def _short(branch):
    return str(branch).split(" / ")[0] if branch else "All branches"


def _status_line(run):
    if run is None:
        return pill("No refresh yet")
    finished = pd.Timestamp(run["finished_ist"])
    return (pill(":material/fiber_manual_record: Live", live=True)
            + pill(f"Stock {pd.Timestamp(run['stock_as_of']):%d %b}")
            + pill(f"Sales to {pd.Timestamp(run['sales_to']):%d %b}")
            + pill(f"Refreshed {finished:%d %b, %H:%M}"))


def _card(a, who):
    label, icon = KINDS.get(a["kind"], (a["kind"], ":material/bolt:"))
    age = (pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None) - pd.Timestamp(a["first_seen_ist"])).days
    with st.container(border=True):
        left, right = st.columns([0.74, 0.26])
        with left:
            meta = [f"{icon} **{label}**", _short(a["branch"]), CONFIDENCE.get(a["confidence"], ""),
                    "new today" if age < 1 else f"open {age} d", f"seen {a['times_seen']}×"]
            st.markdown(" · ".join(m for m in meta if m))
            st.markdown(f'<div class="kk-title">{html.escape(a["title"])}</div>', unsafe_allow_html=True)
            st.caption(a["summary"] or "")
        with right:
            change = ""
            if a["first_impact"] and a["impact_value"] and abs(a["impact_value"] - a["first_impact"]) > 1:
                diff = a["impact_value"] - a["first_impact"]
                change = f" · {'▲' if diff > 0 else '▼'} {money(abs(diff))} since first seen"
            st.markdown(f'<div class="kk-impact">{money(a["impact_value"])}</div>'
                        f'<div class="kk-impact-sub">{html.escape(a["impact_label"] or "")}{change}</div>',
                        unsafe_allow_html=True)
        b = st.columns([1, 1, 1, 1, 3])
        disabled = not who
        aid = a["action_id"]
        if a["status"] in ("open", "snoozed"):
            if b[0].button("Approve", key=f"ap{aid}", icon=":material/check:", type="primary", disabled=disabled):
                _decide(aid, "approve", who); st.rerun()
            if b[1].button("Reject", key=f"rj{aid}", icon=":material/close:", disabled=disabled):
                _decide(aid, "reject", who); st.rerun()
            if a["status"] == "open" and b[2].button("Snooze 7 d", key=f"sz{aid}", icon=":material/snooze:", disabled=disabled):
                _decide(aid, "snooze", who); st.rerun()
        elif a["status"] == "approved":
            if b[0].button("Mark done", key=f"dn{aid}", icon=":material/task_alt:", type="primary", disabled=disabled):
                _decide(aid, "done", who); st.rerun()
            st.caption(f"Approved by {a['decided_by']} on {pd.Timestamp(a['decided_ist']):%d %b}")
        else:
            if b[0].button("Reopen", key=f"ro{aid}", icon=":material/undo:", disabled=disabled):
                _decide(aid, "reopen", who); st.rerun()
        with b[3].popover("Note", icon=":material/chat:", disabled=disabled):
            note = st.text_area("Note", key=f"nt{aid}", label_visibility="collapsed", placeholder="Why, or what was done")
            if st.button("Save note", key=f"sn{aid}"):
                _decide(aid, "comment", who, note); st.rerun()
        with st.expander("Evidence, pieces and history"):
            ev = a["evidence"] if isinstance(a["evidence"], dict) else json.loads(a["evidence"] or "{}")
            st.json(ev, expanded=False)
            pieces = a["pieces"] if isinstance(a["pieces"], list) else json.loads(a["pieces"] or "[]")
            if pieces:
                st.dataframe(pd.DataFrame(pieces), hide_index=True, width="stretch")
            hist = _read("""SELECT at AT TIME ZONE 'Asia/Kolkata' AS at, by, event, note FROM brain.action_event
                            WHERE action_id = :a ORDER BY event_id DESC""", a=aid)
            st.dataframe(hist, hide_index=True, width="stretch")


def render():
    who = st.session_state.get("who", "").strip()
    run = _read("""SELECT run_id, finished_at AT TIME ZONE 'Asia/Kolkata' AS finished_ist, stock_as_of, sales_to
                   FROM brain.run_log WHERE status = 'success' ORDER BY run_id DESC LIMIT 1""")
    run = run.iloc[0] if len(run) else None

    head, button = st.columns([0.8, 0.2])
    head.title("Decisions")
    head.markdown(_status_line(run), unsafe_allow_html=True)
    if button.button("Refresh decisions", icon=":material/refresh:", width="stretch"):
        from kkj.brain.run import refresh
        with st.spinner("Re-reading stock and sales, re-running every rule..."):
            r = refresh("dashboard")
        st.toast(f"{r['created']} new · {r['updated']} updated · {r['resolved']} closed")
        st.rerun()

    actions = _read("""SELECT action_id, kind, title, summary, branch, impact_value, impact_label, first_impact,
                              confidence, evidence, pieces, status, times_seen, decided_by,
                              decided_at AT TIME ZONE 'Asia/Kolkata' AS decided_ist,
                              first_seen_at AT TIME ZONE 'Asia/Kolkata' AS first_seen_ist,
                              last_seen_at AT TIME ZONE 'Asia/Kolkata' AS last_seen_ist, priority
                       FROM brain.action ORDER BY priority DESC, impact_value DESC""")
    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    open_ = actions[actions["status"] == "open"]
    closed_week = _read("""SELECT count(*) AS n FROM brain.action_event WHERE event IN ('done', 'condition_cleared',
                           'auto_resolved') AND at > now() - interval '7 days'""")["n"].iloc[0]
    c = st.columns(4)
    c[0].metric("To decide", f"{len(open_)}", f"{(open_['confidence'] == 'high').sum()} with high confidence",
                delta_color="off")
    c[1].metric("New in the last 24 h", f"{(pd.to_datetime(open_['first_seen_ist']) > now - pd.Timedelta(hours=24)).sum()}",
                delta_color="off")
    c[2].metric("Approved, in progress", f"{(actions['status'] == 'approved').sum()}", delta_color="off")
    c[3].metric("Closed this week", f"{closed_week}", "done or cleared by itself", delta_color="off")
    if not who:
        st.info("Type your name in the sidebar to approve, reject or snooze decisions.", icon=":material/badge:")

    f = st.columns([3, 2, 2])
    group = f[0].segmented_control("Type", list(GROUPS), default="All", key="dec_group")
    view = f[1].segmented_control("Show", list(VIEWS), default="To decide", key="dec_view")
    branches = sorted({_short(b) for b in actions["branch"].dropna()})
    branch = f[2].multiselect("Branch", branches, key="dec_branch")
    shown = actions[actions["status"].isin(VIEWS[view or "To decide"])]
    if GROUPS[group or "All"]:
        shown = shown[shown["kind"].isin(GROUPS[group])]
    if branch:
        shown = shown[shown["branch"].map(_short).isin(branch)]
    st.caption(f"{len(shown)} decisions · ranked by money involved, confidence and urgency "
               "(impacts overlap between types, so they are not added up)")

    limit = st.session_state.get("dec_limit", PAGE)
    for _, a in shown.head(limit).iterrows():
        _card(a, who)
    if len(shown) > limit and st.button(f"Show {min(PAGE, len(shown) - limit)} more"):
        st.session_state["dec_limit"] = limit + PAGE
        st.rerun()

    with st.expander("Activity", icon=":material/history:"):
        st.dataframe(_read("""SELECT e.at AT TIME ZONE 'Asia/Kolkata' AS at, e.by, e.event, a.title, e.note
                              FROM brain.action_event e JOIN brain.action a USING (action_id)
                              WHERE e.event NOT IN ('created') OR e.at > now() - interval '1 day'
                              ORDER BY e.event_id DESC LIMIT 100"""), hide_index=True, width="stretch")
