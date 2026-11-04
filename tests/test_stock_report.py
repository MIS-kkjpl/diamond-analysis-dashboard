"""
Stock export cleaning: the port must reproduce the original pipeline exactly.

Each real ERP export in data/stock/raw/ is cleaned with kkj.erp.stock_report and
compared, cell for cell, with the parquet the original Phase_one.py wrote for the
same run (data/stock/clean/). Every validation check must pass. Skipped when the
data folder is absent (data/ is not in git).
"""
import pandas as pd
import pytest

from kkj import settings
from kkj.erp.stock_report import clean_export, data_quality
from kkj.pipelines.stock import files_for_raw

RAW_FILES = sorted(settings.STOCK_RAW_DIR.glob("*.xlsx")) if settings.STOCK_RAW_DIR.exists() else []


@pytest.mark.skipif(not RAW_FILES, reason="no raw exports in data/stock/raw")
@pytest.mark.parametrize("raw_path", RAW_FILES, ids=[p.stem for p in RAW_FILES])
def test_clean_export_matches_original(raw_path, tmp_path):
    name, stamp, meta_path = files_for_raw(raw_path)
    df, checks, totals = clean_export(raw_path, meta_path)

    failed = [f"{c['check']} ({c['detail']})" for c in checks if not c["passed"]]
    assert not failed, failed
    assert len(df) == totals["count"]

    original_path = settings.STOCK_CLEAN_DIR / f"{name}_{stamp}.parquet"
    if not original_path.exists():
        pytest.skip(f"no original clean copy for {stamp}")
    # Round-trip through parquet so both sides have the same storage types
    df.to_parquet(tmp_path / "port.parquet", index=False)
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "port.parquet"),
                                  pd.read_parquet(original_path))

    warnings = data_quality(df)
    assert set(warnings) == {"null_labeling_date", "zero_item_amt_not_customer",
                             "customer_items", "purity_outside_karat_ranges"}
