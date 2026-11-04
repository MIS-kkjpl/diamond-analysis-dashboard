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
