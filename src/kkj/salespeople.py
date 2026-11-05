"""
Salesperson list (config.salesperson): build it from the sales lines and an HR user list.

The ERP writes the salesperson on each sales line as "<code> <name>", with codes in
mixed forms ("57 Deepak Dholke", "K1912 Samiksha Malve", "k1741 ..."). normalise_code()
turns them into the HR form (K0057). seed() adds every salesperson seen in sales
that is not in the table yet; rows already there (including manual edits) are never
changed.

Run:  python -m kkj.salespeople --hr-file <users.xlsx>   (columns Ecode, Display name)
"""
import argparse
import re
import sys
from pathlib import Path
from difflib import SequenceMatcher

import pandas as pd
from sqlalchemy import text

from kkj import settings

NAME_AGREES = 0.6        # code matches and names this similar -> 'matched'
NAME_MATCH = 0.85        # no code match, but a name this similar -> 'name_match'
OWNER_SURNAMES = ("KOTHARI",)
GENERIC_CODES = ("EXB",)


def normalise_code(raw):
    """'57' -> 'K0057', 'k1741' -> 'K1741', '10003' -> 'K10003', 'EXB' -> 'EXB'."""
    code = str(raw).strip().upper()
    if re.fullmatch(r"K?\d+", code):
        return "K" + code.lstrip("K").zfill(4)
    return code


def split_salesman(value):
    """'57 Deepak Dholke' -> ('K0057', 'Deepak Dholke')."""
    m = re.match(r"^\s*(\S+)\s+(.*?)\s*$", str(value))
    return (normalise_code(m.group(1)), re.sub(r"\s+", " ", m.group(2))) if m else (normalise_code(value), str(value))


def similarity(a, b):
    return SequenceMatcher(None, str(a).upper(), str(b).upper()).ratio()


def build(sales, hr):
    """One row per salesperson seen in sales, matched against the HR list.

    sales: DataFrame(salesman, branch, lines); hr: DataFrame(code, name).
    """
    s = sales.copy()
    s[["sales_code", "erp_name"]] = s["salesman"].map(split_salesman).tolist()
    main_branch = (s.sort_values("lines", ascending=False).drop_duplicates("sales_code")
                   .set_index("sales_code")["branch"])
    people = s.groupby("sales_code", as_index=False).agg(erp_name=("erp_name", "first"), lines=("lines", "sum"))
    hr_by_code = hr.drop_duplicates("code").set_index("code")["name"]
    rows = []
    for p in people.itertuples():
        hr_name = hr_by_code.get(p.sales_code)
        row = {"sales_code": p.sales_code, "branch": main_branch.get(p.sales_code), "kind": "salesperson",
               "employee_code": None, "name": p.erp_name.title(), "match_status": "unmapped", "note": None}
        if hr_name is not None and similarity(p.erp_name, hr_name) >= NAME_AGREES:
            row.update(name=hr_name.title(), employee_code=p.sales_code, match_status="matched")
        elif hr_name is not None:
            row["note"] = f"HR list has {p.sales_code} as {hr_name.title()}: check which is right"
        else:
            scores = hr.assign(score=hr["name"].map(lambda n: similarity(p.erp_name, n))).sort_values("score")
            best = scores.iloc[-1] if len(scores) else None
            if best is not None and best["score"] >= NAME_MATCH:
                row.update(name=best["name"].title(), employee_code=best["code"], match_status="name_match",
                           note=f"matched by name; HR code {best['code']}")
        if any(s in p.erp_name.upper() for s in OWNER_SURNAMES):
            row["kind"] = "owner"
            row["note"] = (row["note"] + "; " if row["note"] else "") + "marked owner (surname)"
        if p.sales_code in GENERIC_CODES:
            row["kind"] = "generic"
        rows.append(row)
    return pd.DataFrame(rows)


def read_hr_file(path):
    hr = pd.read_excel(path, dtype=str)
    hr = hr[hr["Ecode"].str.strip().str.match(r"^K\d+$", na=False)]
    return pd.DataFrame({"code": hr["Ecode"].str.strip().map(normalise_code), "name": hr["Display name"].str.strip()})


def seed(conn, people, added_by="seed"):
    """Insert people not in config.salesperson yet; existing rows are left alone. Returns rows added."""
    added = 0
    people = people.astype(object).where(people.notna(), None)      # NaN -> NULL, never the text 'NaN'
    for r in people.itertuples():
        added += conn.execute(text("""
            INSERT INTO config.salesperson (sales_code, name, employee_code, branch, kind, match_status, note, updated_by)
            VALUES (:c, :n, :e, :b, :k, :m, :note, :by) ON CONFLICT (sales_code) DO NOTHING"""),
            {"c": r.sales_code, "n": r.name, "e": r.employee_code, "b": r.branch, "k": r.kind,
             "m": r.match_status, "note": r.note, "by": added_by}).rowcount
    return added


# ── reconciliation with the HR employee list ──────────────────────────────────
SYSTEM_EDITORS = ("seed", "default", "employee list")    # rows changed by anyone else are a person's edits: kept
LOCATION_ALIASES = {"MUMBAI OFFICE": "Mumbai Branch"}
LEFT_AFTER_DAYS = 90       # not on the active list and no sale for this long -> switched off


def read_employee_file(path):
    """The HR employee list (active staff): code, name, status, designation, department, location, joined."""
    hr = pd.read_excel(path, dtype=str)
    hr = hr[hr["Emp. Code"].str.strip().str.match(r"^[Kk]?\d+$", na=False)]
    return pd.DataFrame({"code": hr["Emp. Code"].str.strip().map(normalise_code), "name": hr["Name"].str.strip(),
                         "status": hr["CURRENT STATUS"].str.strip(), "designation": hr["Designation"].str.strip(),
                         "department": hr["Department"].str.strip(), "location": hr["Location"].str.strip(),
                         "joined": pd.to_datetime(hr["DOJ"], errors="coerce").dt.date}).reset_index(drop=True)


def branch_for_location(location, branches):
    """'ITWARI (KJ3)' -> 'KJ3 / Branch Itwari' by config.branch place names; None when not a branch."""
    loc = str(location or "").strip().upper()
    if loc in LOCATION_ALIASES:
        return LOCATION_ALIASES[loc]
    for b in branches.itertuples():
        if b.place and str(b.place).upper() in loc:
            return b.erp_branch
    return None


def _code_number(code):
    m = re.fullmatch(r"K(\d+)", str(code))
    return int(m.group(1)) if m else -1


def _value(v):
    return None if v is None or (not isinstance(v, str) and pd.isna(v)) else v


def reconcile(people, hr, sales, branches, today):
    """Proposed values per salesperson, why, and anything a person should check.

    people: config.salesperson rows; hr: read_employee_file(); sales: per salesperson and branch
    (sales_code, branch, lines, last_sale, recent).
    """
    hr_by_code = hr.drop_duplicates("code").set_index("code")
    # the list is an export of active staff on some date: anyone with a newer code joined after it
    newest_code = max(_code_number(c) for c in hr["code"])
    last = sales.groupby("sales_code")["last_sale"].max()
    sold_at = (sales[sales["recent"]].sort_values("lines", ascending=False).drop_duplicates("sales_code")
               .set_index("sales_code")["branch"])
    out = []
    for p in people.itertuples():
        new = {"employee_code": _value(p.employee_code), "name": p.name, "branch": _value(p.branch),
               "active": bool(p.active), "match_status": p.match_status, "note": _value(p.note)}
        why, check, h, code = [], "", None, None
        if p.sales_code in hr_by_code.index:
            cand = hr_by_code.loc[p.sales_code]
            if similarity(p.name, cand["name"]) >= NAME_AGREES:
                h, code = cand, p.sales_code
            else:
                check = f"code {p.sales_code} is {cand['name'].title()} in the employee list but {p.name} in sales"
        elif p.kind == "salesperson":
            scores = hr.assign(score=hr["name"].map(lambda n: similarity(p.name, n))).sort_values("score")
            if len(scores) and scores.iloc[-1]["score"] >= NAME_MATCH:
                h, code = scores.iloc[-1], scores.iloc[-1]["code"]
                why.append(f"found by name, employee code {code}")
        last_sale = last.get(p.sales_code)
        if h is not None:
            home = branch_for_location(h["location"], branches)
            new.update(employee_code=code, name=h["name"].title(), active=True,
                       match_status="matched" if code == p.sales_code else "name_match")
            if home:
                new["branch"] = home
            joined = f", joined {h['joined']:%b %Y}" if pd.notna(h["joined"]) else ""
            new["note"] = f"HR: {str(h['designation']).title()}, {str(h['department']).title()}, {h['location']}{joined}"
            why.append("on the employee list")
            if p.sales_code in sold_at.index and home and sold_at[p.sales_code] != home:
                check = f"employee list says {h['location']}, but sells mostly at {str(sold_at[p.sales_code]).split(' / ')[0]}"
        elif p.kind == "salesperson" and not check and _code_number(p.sales_code) < 0:
            check = f"'{p.sales_code}' is not an employee code (a counter login?): mark it generic in Settings if so"
        elif p.kind == "salesperson" and not check and _code_number(p.sales_code) > newest_code:
            why.append(f"joined after the employee list was made (its newest code is K{newest_code:04d}); not judged")
        elif p.kind == "salesperson" and not check:
            if last_sale is not None and pd.notna(last_sale) and (today - last_sale).days <= LEFT_AFTER_DAYS:
                check = f"not on the active employee list, but sold on {last_sale:%d %b %Y}"
            else:
                new["active"] = False
                new["note"] = (f"Not on the active employee list ({today:%d %b %Y}); last sale "
                               + (f"{last_sale:%d %b %Y}" if last_sale is not None and pd.notna(last_sale) else "none"))
                why.append(f"not on the employee list, no sale in {LEFT_AFTER_DAYS} days")
        if check:
            new["note"] = f"CHECK: {check}"
        changes = {k: (_value(getattr(p, k)), v) for k, v in new.items() if _value(getattr(p, k)) != v}
        out.append({"sales_code": p.sales_code, "name_in_sales": p.name, "kind": p.kind,
                    "edited_by_person": p.updated_by not in SYSTEM_EDITORS, "last_sale": last_sale,
                    "why": "; ".join(why), "check": check, "changes": changes,
                    **{f"new_{k}": v for k, v in new.items()}})
    return pd.DataFrame(out)


def apply(conn, plan, by):
    """Write the proposed values (never to rows a person edited); each change goes into config.change_log."""
    import json
    n = 0
    for r in plan.itertuples():
        if r.edited_by_person or not r.changes:
            continue
        sets = {k: v for k, (_, v) in r.changes.items()}
        conn.execute(text("UPDATE config.salesperson SET " + ", ".join(f"{k} = :{k}" for k in sets)
                          + ", updated_at = now(), updated_by = 'employee list' WHERE sales_code = :code"),
                     {**sets, "code": r.sales_code})
        conn.execute(text("""INSERT INTO config.change_log (changed_by, table_name, row_key, old_value, new_value, note)
                             VALUES (:by, 'salesperson', :k, CAST(:o AS jsonb), CAST(:n AS jsonb), :note)"""),
                     {"by": by, "k": r.sales_code, "n": json.dumps(sets, default=str),
                      "o": json.dumps({k: o for k, (o, _) in r.changes.items()}, default=str),
                      "note": r.why or r.check or None})
        n += 1
    return n


def run_reconcile(path, dry_run=False):
    from datetime import date, datetime
    today = date.today()
    hr = read_employee_file(path)
    with settings.app_engine().begin() as conn:
        people = pd.read_sql(text("SELECT * FROM config.salesperson ORDER BY sales_code"), conn)
        branches = pd.read_sql(text("SELECT erp_branch, place FROM config.branch"), conn)
        sales = pd.read_sql(text("""SELECT salesman, branch, count(*) AS lines, max(sale_date) AS last_sale,
                                           bool_or(sale_date >= current_date - 180) AS recent
                                    FROM sales.v_sales_line WHERE salesman IS NOT NULL GROUP BY 1, 2"""), conn)
        sales["sales_code"] = sales["salesman"].map(lambda v: split_salesman(v)[0])
        plan = reconcile(people, hr, sales, branches, today)
        written = 0 if dry_run else apply(conn, plan, f"Employee list ({Path(path).stem})")
    out_dir = settings.PROJECT_DIR / "data" / "reconcile"
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx = out_dir / f"salespeople_vs_employees_{datetime.now():%Y%m%d_%H%M}.xlsx"
    sheet = plan.assign(changes=plan["changes"].map(lambda c: "; ".join(f"{k}: {o} -> {n}" for k, (o, n) in c.items())))
    with pd.ExcelWriter(xlsx) as w:
        sheet[sheet["check"] != ""].to_excel(w, sheet_name="To check", index=False)
        sheet.to_excel(w, sheet_name="All salespeople", index=False)
        hr.to_excel(w, sheet_name="Employee list", index=False)
    print(f"{len(plan)} salespeople: {int((plan['changes'].map(len) > 0).sum())} with changes "
          f"({'dry run, nothing written' if dry_run else f'{written} written'}), "
          f"{int((plan['check'] != '').sum())} to check, {int(plan['edited_by_person'].sum())} edited by a person "
          f"(left alone). Review: {xlsx}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add salespeople seen in sales to config.salesperson, or reconcile "
                                             "them with the HR employee list.")
    ap.add_argument("--hr-file", help="HR user list .xlsx (columns Ecode, Display name): add new salespeople")
    ap.add_argument("--employee-file", help="HR employee list .xlsx (Emp. Code, Name, Location, ...): reconcile")
    ap.add_argument("--dry-run", action="store_true", help="with --employee-file: write the review sheet only")
    args = ap.parse_args(argv)
    if args.employee_file:
        sys.exit(run_reconcile(args.employee_file, args.dry_run))
    if not args.hr_file:
        ap.error("give --hr-file or --employee-file")
    engine = settings.app_engine()
    with engine.begin() as conn:
        sales = pd.read_sql(text("""SELECT salesman, branch, count(*) AS lines FROM sales.sales_line
                                    WHERE salesman IS NOT NULL GROUP BY 1, 2"""), conn)
        people = build(sales, read_hr_file(args.hr_file))
        added = seed(conn, people)
    print(f"{len(people)} salespeople in sales; {added} added. By status: "
          f"{people['match_status'].value_counts().to_dict()}; by kind: {people['kind'].value_counts().to_dict()}")
    sys.exit(0)


if __name__ == "__main__":
    main()
