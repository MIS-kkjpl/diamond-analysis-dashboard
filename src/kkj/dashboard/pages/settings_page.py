"""Settings: the business rules the recommendations run on, and the salesperson list."""
import json

import pandas as pd
import streamlit as st
from sqlalchemy import text

from kkj import rules, settings


@st.cache_resource
def _app_engine():
    return settings.app_engine()


def _rule_input(rule, value):
    """The right input for one rule; returns the edited value."""
    label = f"{rule.label}" + (f" ({rule.unit})" if rule.unit else "")
    key = f"rule_{rule.key}"
    if rule.kind == "bool":
        return st.toggle(label, value=bool(value), help=rule.help, key=key)
    if rule.kind == "int":
        return int(st.number_input(label, value=int(value), step=1, help=rule.help, key=key,
                                   min_value=int(rule.min) if rule.min is not None else None,
                                   max_value=int(rule.max) if rule.max is not None else None))
    if rule.kind == "number":
        return float(st.number_input(label, value=float(value), help=rule.help, key=key,
                                     min_value=float(rule.min) if rule.min is not None else None,
                                     max_value=float(rule.max) if rule.max is not None else None))
    if rule.kind == "choice":
        return st.selectbox(label, rule.choices, index=list(rule.choices).index(value), help=rule.help, key=key)
    if rule.choices:                                   # list chosen from fixed options
        return st.multiselect(label, rule.choices, default=list(value), help=rule.help, key=key)
    raw = st.text_input(label, value=", ".join(str(v) for v in value), help=rule.help + " Comma-separated.", key=key)
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if rule.default and all(isinstance(d, (int, float)) for d in rule.default):
        return [float(x) if "." in x else int(x) for x in items]
    return items


def _branches():
    with _app_engine().connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT DISTINCT branch FROM erp.stock_snapshot WHERE branch IS NOT NULL ORDER BY 1")).all()]


def render():
    st.title("Settings")
    st.caption("The rules every recommendation is calculated from. Defaults are starting points: tune them to "
               "how the business wants to run. Every change is recorded below with your name.")
    who = st.text_input("Your name (recorded with any change you save)", key="changed_by").strip()

    with _app_engine().connect() as c:
        current = rules.load(c)

    sections = list(dict.fromkeys(r.section for r in rules.RULES))
    for section in sections:
        with st.expander(section, expanded=section == "Targets"):
            edited = {}
            for r in [r for r in rules.RULES if r.section == section]:
                if r.key == "transfer_excluded_branches":
                    edited[r.key] = st.multiselect(r.label, _branches(), default=current[r.key], help=r.help,
                                                   key=f"rule_{r.key}")
                else:
                    edited[r.key] = _rule_input(r, current[r.key])
            if st.button(f"Save {section.lower()}", key=f"save_{section}", disabled=not who):
                with _app_engine().begin() as c:
                    changed = [k for k, v in edited.items() if rules.save(c, k, v, who)]
                st.cache_data.clear()
                st.success(f"Saved: {', '.join(rules.BY_KEY[k].label for k in changed)}" if changed else "No changes.")
    if not who:
        st.caption("Type your name above to enable saving.")

    st.subheader("Salespeople")
    st.caption("Everyone who appears on a sales line. Fix names, mark people who have left, and set the type: "
               "owners and generic accounts (e.g. exhibition sales) are left out of salesperson comparisons.")
    with _app_engine().connect() as c:
        people = pd.read_sql(text("""SELECT sales_code, name, employee_code, branch, kind, active, left_on,
                                     match_status, note FROM config.salesperson
                                     ORDER BY (match_status = 'matched'), name"""), c)
    review = people[people["match_status"].isin(["unmapped", "name_match"])]
    st.caption(f"{len(people)} people · {len(review)} to review (not matched to the HR list by code).")
    edited = st.data_editor(
        people, hide_index=True, width="stretch", key="people_editor", disabled=["sales_code", "match_status"],
        column_config={
            "sales_code": st.column_config.TextColumn("Code in sales"),
            "kind": st.column_config.SelectboxColumn("Type", options=["salesperson", "owner", "generic"], required=True),
            "active": st.column_config.CheckboxColumn("Active"),
            "left_on": st.column_config.DateColumn("Left on", format="DD/MM/YYYY"),
            "match_status": st.column_config.TextColumn("Match"),
        })
    if st.button("Save salespeople", disabled=not who):
        before = people.set_index("sales_code")
        after = edited.set_index("sales_code")
        cols = ["name", "employee_code", "branch", "kind", "active", "left_on", "note"]
        changed = [code for code in after.index
                   if not before.loc[code, cols].astype(str).equals(after.loc[code, cols].astype(str))]
        with _app_engine().begin() as c:
            for code in changed:
                row = after.loc[code]
                c.execute(text("""UPDATE config.salesperson SET name = :name, employee_code = :ec, branch = :branch,
                                  kind = :kind, active = :active, left_on = :left_on, note = :note,
                                  match_status = CASE WHEN match_status = 'matched' THEN 'matched' ELSE 'manual' END,
                                  updated_at = now(), updated_by = :by WHERE sales_code = :code"""),
                          {"name": row["name"], "ec": row["employee_code"] or None, "branch": row["branch"],
                           "kind": row["kind"], "active": bool(row["active"]),
                           "left_on": None if pd.isna(row["left_on"]) else row["left_on"],
                           "note": row["note"] or None, "by": who, "code": code})
                c.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value)
                                  VALUES (:by, 'salesperson', :code, CAST(:old AS jsonb), CAST(:new AS jsonb))"""),
                          {"by": who, "code": code,
                           "old": json.dumps(before.loc[code, cols].astype(str).to_dict()),
                           "new": json.dumps(after.loc[code, cols].astype(str).to_dict())})
        st.cache_data.clear()
        st.success(f"Saved {len(changed)} change(s)." if changed else "No changes.")

    st.subheader("Branches")
    st.caption("How each ERP branch is treated. Showrooms are compared with each other and exchange stock; "
               "head office, wholesale and exhibitions are shown but kept out of showroom comparisons.")
    with _app_engine().connect() as c:
        branches = pd.read_sql(text("""SELECT erp_branch, short_name, hr_code, place, kind, active
                                       FROM config.branch ORDER BY kind, short_name"""), c)
    edited_b = st.data_editor(
        branches, hide_index=True, width="stretch", key="branch_editor", disabled=["erp_branch"],
        column_config={"erp_branch": st.column_config.TextColumn("Branch in the ERP"),
                       "kind": st.column_config.SelectboxColumn(
                           "Type", options=["showroom", "head_office", "wholesale", "exhibition", "other"], required=True),
                       "active": st.column_config.CheckboxColumn("Active")})
    if st.button("Save branches", disabled=not who):
        before, after = branches.set_index("erp_branch"), edited_b.set_index("erp_branch")
        cols = ["short_name", "hr_code", "place", "kind", "active"]
        changed = [b for b in after.index if not before.loc[b, cols].astype(str).equals(after.loc[b, cols].astype(str))]
        with _app_engine().begin() as c:
            for b in changed:
                row = after.loc[b]
                c.execute(text("""UPDATE config.branch SET short_name = :s, hr_code = :h, place = :p, kind = :k,
                                  active = :a, updated_at = now(), updated_by = :by WHERE erp_branch = :b"""),
                          {"s": row["short_name"], "h": row["hr_code"] or None, "p": row["place"], "k": row["kind"],
                           "a": bool(row["active"]), "by": who, "b": b})
                c.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value)
                                  VALUES (:by, 'branch', :b, CAST(:old AS jsonb), CAST(:new AS jsonb))"""),
                          {"by": who, "b": b, "old": json.dumps(before.loc[b, cols].astype(str).to_dict()),
                           "new": json.dumps(after.loc[b, cols].astype(str).to_dict())})
        st.cache_data.clear()
        st.success(f"Saved {len(changed)} change(s)." if changed else "No changes.")

    st.subheader("Change history")
    with _app_engine().connect() as c:
        log = pd.read_sql(text("""SELECT changed_at AT TIME ZONE 'Asia/Kolkata' AS changed_at_ist, changed_by,
                                  table_name, row_key, old_value, new_value, note
                                  FROM config.change_log ORDER BY change_id DESC LIMIT 200"""), c)
    st.dataframe(log, hide_index=True, width="stretch")
