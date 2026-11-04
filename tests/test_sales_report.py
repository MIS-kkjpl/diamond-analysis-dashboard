"""Sales report cleaning and range splitting, on synthetic ERP responses (no ERP, no DB)."""
import datetime as dt
import glob

import pandas as pd
import pytest

from kkj.erp.sales_report import clean, data_quality, load_raw
from kkj.pipelines.sales import month_chunks


def saved(rows, totals):
    return {"params": {"fromDate": "01/11/2026", "toDate": "03/11/2026"},
            "stepA": {"data": {"reportID": "R1", "serverTotal": totals}}, "rows": rows}


SALE = {"date": "02/11/2026", "billno": "MK2SB-1", "docno": "MK2SB-1", "lblno": 15180, "barcode": "DTP15180",
        "itemname": "D TOPS", "groupname": "Diamond Jewellery", "pcs": 1.0, "netwt": 2.5, "diawt": 0.4,
        "itemamt": 100000.0, "taxableamount": 93000.0, "diapcs": "29", "orglbldate": "02/05/2025",
        "lbldt": "02/05/2025", "daysfromlabeling": 549, "itemInvOwnCode": 42718264, "salesReturnDoneFlag": False}
RETURN = {"date": "04/11/2026", "billno": "MK2SR-236", "docno": "MK2SR-236", "lblno": 0,
          "itemname": "D NOSE PIN", "groupname": "Diamond Jewellery", "pcs": -1.0, "netwt": -0.32,
          "diawt": -0.1, "itemamt": -12119.0, "diapcs": "-1", "lbldt": "24/06/2026",
          "daysfromlabeling": 197, "itemInvOwnCode": 38402964, "salesReturnDoneFlag": True}


def test_clean_sale_and_return_rows():
    totals = {"recordCount": 2, "pcs": 0.0, "itemamt": 87881.0, "netwt": 2.18, "diawt": 0.3}
    df, checks, _ = clean(saved([SALE, RETURN], totals))
    assert all(c["passed"] for c in checks), [c for c in checks if not c["passed"]]
    assert df["sale_date"].tolist() == [dt.date(2026, 11, 2), dt.date(2026, 11, 4)]
    assert df["dia_pcs"].tolist() == [29, -1]
    assert pd.isna(df.loc[1, "labeling_date"])          # absent in the JSON -> missing, not "nan"
    assert df["sales_return_done"].tolist() == [False, True]
    dq = data_quality(df)
    assert dq["return_lines"] == 1 and dq["returns_without_taxable_amount"] == 1
    assert dq["bill_types"] == {"SB": 1, "SR": 1}


def test_totals_mismatch_fails():
    df, checks, _ = clean(saved([SALE], {"recordCount": 2, "pcs": 1.0, "itemamt": 100000.0,
                                         "netwt": 2.5, "diawt": 0.4}))
    failed = [c["check"] for c in checks if not c["passed"]]
    assert failed == ["Rows == step A recordCount"]


def test_sale_without_barcode_fails():
    df, checks, _ = clean(saved([{**SALE, "barcode": None}], {"recordCount": 1, "pcs": 1.0, "itemamt": 100000.0,
                                                              "netwt": 2.5, "diawt": 0.4}))
    assert not next(c for c in checks if "barcode" in c["check"])["passed"]


def test_month_chunks():
    assert month_chunks(dt.date(2026, 4, 15), dt.date(2026, 6, 3)) == [
        (dt.date(2026, 4, 15), dt.date(2026, 4, 30)),
        (dt.date(2026, 5, 1), dt.date(2026, 5, 31)),
        (dt.date(2026, 6, 1), dt.date(2026, 6, 3))]


SAVED = sorted(glob.glob("data/sales/raw/*.json.gz"))


@pytest.mark.skipif(not SAVED, reason="no saved ERP sales responses in data/sales/raw")
@pytest.mark.parametrize("path", SAVED)
def test_saved_responses_pass_checks(path):
    df, checks, _ = clean(load_raw(path))
    assert all(c["passed"] for c in checks), [c for c in checks if not c["passed"]]
