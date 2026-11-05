"""Salesperson code normalising and matching against the HR list."""
import pandas as pd

from kkj import rules
from kkj.salespeople import build, normalise_code, split_salesman


def test_normalise_code():
    assert [normalise_code(c) for c in ["57", "k1741", "K1912", "10003", "EXB"]] == [
        "K0057", "K1741", "K1912", "K10003", "EXB"]
    assert split_salesman("57 Deepak  Dholke") == ("K0057", "Deepak Dholke")


def test_build_matches_by_code_then_name():
    sales = pd.DataFrame({"salesman": ["57 Deepak Dholke", "1489 TUSHAR SONI", "K1715 Pratibha Bawangade",
                                       "10003 PRADEEP KOTHARI", "EXB SALES", "K9999 New Person"],
                          "branch": ["KJ2", "KJ2", "KJ7", "KJ2", "Exhibition Branch", "KJ3"],
                          "lines": [10, 5, 3, 7, 4, 1]})
    hr = pd.DataFrame({"code": ["K0057", "K1489", "K1722"],
                       "name": ["DEEPAK RAMCHANDRA DHOLKE", "TUSHAR ARMARKAR", "PRATIBHA BAVANGADE"]})
    out = build(sales, hr).set_index("sales_code")
    assert out.loc["K0057", "match_status"] == "matched"
    assert out.loc["K1489", "match_status"] == "unmapped" and "check" in out.loc["K1489", "note"]
    assert out.loc["K1715", "match_status"] == "name_match" and out.loc["K1715", "employee_code"] == "K1722"
    assert out.loc["K10003", "kind"] == "owner"
    assert out.loc["EXB", "kind"] == "generic"
    assert out.loc["K9999", "match_status"] == "unmapped"


def test_rule_defaults_are_complete():
    assert len({r.key for r in rules.RULES}) == len(rules.RULES)
    for r in rules.RULES:
        assert r.section and r.label and r.default is not None
        if r.kind == "choice":
            assert r.default in r.choices


def test_reconcile_with_the_employee_list():
    from datetime import date
    import pandas as pd
    from kkj.salespeople import reconcile
    hr = pd.DataFrame({"code": ["K0057", "K0100", "K1489"], "name": ["DEEPAK DHOLKE", "ASHA RAO", "TUSHAR ARMARKAR"],
                       "status": "ACTIVE", "designation": "EXECUTIVE", "department": "RETAIL",
                       "location": ["ITWARI (KJ3)", "DHARAMPETH", "AMRAVATI"], "joined": [date(2015, 1, 1)] * 3})
    people = pd.DataFrame({"sales_code": ["K0057", "K0200", "K1489", "K0300", "K0400", "KJ3"],
                           "name": ["Deepak Dholke", "Asha Rao", "Tushar Soni", "Gone Person", "Recent Person", "Counter"],
                           "employee_code": None, "branch": None, "kind": "salesperson", "active": True,
                           "match_status": "unmapped", "note": None,
                           "updated_by": ["seed", "seed", "seed", "seed", "seed", "seed"]})
    sales = pd.DataFrame({"sales_code": ["K0057", "K0300", "K0400"], "branch": ["KJ2 / Branch Dharampeth"] * 3,
                          "lines": [10, 1, 1], "recent": [True, False, True],
                          "last_sale": [date(2026, 11, 1), date(2026, 1, 5), date(2026, 10, 30)]})
    branches = pd.DataFrame({"erp_branch": ["KJ2 / Branch Dharampeth", "KJ3 / Branch Itwari"], "place": ["Dharampeth", "Itwari"]})
    plan = reconcile(people, hr, sales, branches, date(2026, 11, 5)).set_index("sales_code")
    assert plan.loc["K0057", "new_branch"] == "KJ3 / Branch Itwari" and plan.loc["K0057", "new_match_status"] == "matched"
    assert "sells mostly at KJ2" in plan.loc["K0057", "check"]                     # HR home vs where they sell
    assert plan.loc["K0200", "new_employee_code"] == "K0100" and plan.loc["K0200", "new_match_status"] == "name_match"
    assert "Tushar Armarkar" in plan.loc["K1489", "check"]                          # same code, different person
    assert not plan.loc["K0300", "new_active"]                                     # not on the list, no sale in 90 days
    assert plan.loc["K0400", "new_active"] and "sold on" in plan.loc["K0400", "check"]
    assert plan.loc["KJ3", "new_active"] and "not an employee code" in plan.loc["KJ3", "check"]
