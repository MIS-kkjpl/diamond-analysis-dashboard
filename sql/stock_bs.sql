-- Stock in Business Studio's 7 categories (diamond, kundan, loose, platinum, stone, stone mala, watch).
-- Filled by python -m kkj.pipelines.stock_bs from several ERP pulls (config/stock_categories.json).
-- Runs side by side with erp.stock_snapshot until signed off; same columns plus the category and the pull.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS erp.stock_snapshot_bs (
    LIKE erp.stock_snapshot INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
    stock_category text NOT NULL,
    pull           text NOT NULL,
    PRIMARY KEY (snapshot_date, barcode)
);
-- Business Studio's value: ERP Item Amt + Other Charges
ALTER TABLE erp.stock_snapshot_bs ADD COLUMN IF NOT EXISTS stock_value numeric(16,2);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_bs_category ON erp.stock_snapshot_bs (snapshot_date, stock_category);
GRANT SELECT ON erp.stock_snapshot_bs TO powerbi_reader;
RESET ROLE;
GRANT SELECT ON erp.stock_snapshot_bs TO dashboard_app;
