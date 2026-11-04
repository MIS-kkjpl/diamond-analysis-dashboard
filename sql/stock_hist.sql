-- Schema stock_hist: stock as it stood on PAST dates (back-dated ERP reports and
-- the legacy snapshot CSVs). Kept apart from erp.stock_snapshot, which only ever
-- holds same-day production snapshots.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- Can be dropped and reloaded freely: DROP SCHEMA stock_hist CASCADE;

CREATE SCHEMA IF NOT EXISTS stock_hist AUTHORIZATION erp_etl;
SET ROLE erp_etl;

-- One row per snapshot file considered, loaded or not, and why
CREATE TABLE IF NOT EXISTS stock_hist.snapshot_status (
    snapshot_date   date NOT NULL,
    source          text NOT NULL CHECK (source IN ('csv_legacy', 'erp_asof')),
    file            text NOT NULL,
    status          text NOT NULL CHECK (status IN ('loaded', 'quarantined')),
    reason          text,
    labels          int,
    checks          jsonb,             -- every integrity check with its numbers
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, source)
);

-- Same column names as erp.stock_snapshot, so the same queries work on both.
CREATE TABLE IF NOT EXISTS stock_hist.snapshot (
    snapshot_date        date          NOT NULL,
    source               text          NOT NULL CHECK (source IN ('csv_legacy', 'erp_asof')),
    sr_no                int,
    label_no             int,
    category             text,
    item_class_group     text,
    branch               text,          -- legacy CSVs lack it; filled from the location map
    location             text,
    doc_no               text,
    barcode              text          NOT NULL,
    item_name            text,
    set_item_barcode     text,
    purity               numeric(6,2),
    pcs                  numeric(10,3),
    gross_wt             numeric(12,3),
    net_wt               numeric(12,3),
    dia_pcs              int,
    dia_wt               numeric(12,3),
    cst_wt               numeric(12,3),
    dia_amt              numeric(16,2),
    stone_amt            numeric(16,2),
    item_amt             numeric(16,2),
    cst_amt              numeric(16,2),
    labour_rate          numeric(12,2),
    labour_charges       numeric(14,2),
    age_days             int,           -- recomputed: snapshot_date - coalesce(label_dt, labeling_date)
    age_days_reported    int,           -- as in the file (legacy files: age on the EXPORT day)
    order_no             text,
    lot_no               text,
    latest_txn_date      date,
    supplier             text,
    value_by_todays_rate numeric(16,2),
    fine_wt              numeric(12,3),
    rate                 numeric(12,2),
    labour_type          text,
    labeling_date        date,
    wastage_per          numeric(8,3),
    stone_wt             numeric(12,3),
    sub_category         text,
    label_dt             date,
    wastage_amount       numeric(14,2),
    scm_item             text,
    branch_in_date       date,
    days_from_branch_in  int,
    taxable_amount       numeric(16,2),
    other_charges        numeric(14,2),
    loaded_at            timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, source, barcode)
);

GRANT USAGE ON SCHEMA stock_hist TO powerbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA stock_hist TO powerbi_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA stock_hist
    GRANT SELECT ON TABLES TO powerbi_reader;

RESET ROLE;
