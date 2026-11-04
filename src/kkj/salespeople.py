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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add salespeople seen in sales to config.salesperson.")
    ap.add_argument("--hr-file", required=True, help="HR user list .xlsx (columns Ecode, Display name)")
    args = ap.parse_args(argv)
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
