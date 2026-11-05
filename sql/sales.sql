-- Schema sales: one row per sold label line, from the ERP sales report
-- (Criteria-wise Sales) and, for older periods, the legacy sales CSV.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- Separate from schema erp (production stock) so it can be dropped and reloaded
-- freely: DROP SCHEMA sales CASCADE; then re-run setup.

CREATE SCHEMA IF NOT EXISTS sales AUTHORIZATION erp_etl;
SET ROLE erp_etl;

-- One row per load (a date range from one source)
CREATE TABLE IF NOT EXISTS sales.load_log (
    load_id       serial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    source        text NOT NULL CHECK (source IN ('erp', 'csv_legacy')),
    date_from     date NOT NULL,
    date_to       date NOT NULL,
    report_id     text,
    raw_file      text,
    record_count  int,                -- what the source says it holds
    rows_loaded   int,
    validation    jsonb,
    dq_warnings   jsonb,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'success', 'failed')),
    error         text
);

CREATE TABLE IF NOT EXISTS sales.sales_line (
    line_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source                text   NOT NULL CHECK (source IN ('erp', 'csv_legacy')),
    sale_date             date   NOT NULL,
    bill_no               text,
    doc_no                text,
    label_no              int,
    barcode               text,
    barcode_rebuilt       boolean NOT NULL DEFAULT false,  -- legacy May–Dec 2025: file held the bill no;
                                                           -- rebuilt as item prefix + label no
    set_item_barcode      text,
    item_name             text,
    category              text,
    sub_category          text,
    item_class_group      text,
    branch                text,          -- ERP only; legacy rows get it from the location map
    location              text,
    salesman              text,          -- ERP only
    supplier              text,
    purity                numeric(6,2),
    pcs                   numeric(10,3),
    gross_wt              numeric(12,3),
    net_wt                numeric(12,3),
    fine_wt               numeric(12,3),
    dia_pcs               int,
    dia_wt                numeric(12,3),
    cst_wt                numeric(12,3),
    stone_wt              numeric(12,3),
    dia_amt               numeric(16,2),
    stone_amt             numeric(16,2),
    cst_amt               numeric(16,2),
    labour_rate           numeric(12,2),
    labour_charges        numeric(14,2),
    wastage_per           numeric(8,3),
    wastage_amount        numeric(14,2),
    rate                  numeric(12,2),
    item_amt              numeric(16,2),   -- label value at the sale-day rate
    taxable_amount        numeric(16,2),   -- ERP only; billed value (basis to be confirmed)
    other_charges         numeric(14,2),   -- ERP only
    age_days_at_sale      int,             -- days from labeling to sale
    days_from_branch_in   int,             -- ERP only
    labeling_date         date,
    label_dt              date,
    branch_in_date        date,            -- ERP only
    latest_txn_date       date,
    order_no              text,
    labour_type           text,
    item_code             int,             -- ERP only: internal ids, stable across relabels
    batch_code            bigint,
    item_inv_own_code     bigint,
    inventory_ref_code    bigint,          -- ERP only: the sale line's id; a return carries its sale's id
    orig_sale_date        date,            -- ERP only: date of the sale (for a return: of the sale it reverses).
                                           -- The ERP report selects lines by this date, so loads replace by it
    orig_bill_no          text,            -- ERP only: bill of that sale
    sales_return_done     boolean,         -- ERP only
    raw                   jsonb,           -- ERP only: the full original row
    load_id               int REFERENCES sales.load_log (load_id),
    loaded_at             timestamptz NOT NULL DEFAULT now()
);

-- For tables created before barcode_rebuilt existed
ALTER TABLE sales.sales_line ADD COLUMN IF NOT EXISTS barcode_rebuilt boolean NOT NULL DEFAULT false;

ALTER TABLE sales.sales_line ADD COLUMN IF NOT EXISTS inventory_ref_code bigint;
ALTER TABLE sales.sales_line ADD COLUMN IF NOT EXISTS orig_sale_date date;
ALTER TABLE sales.sales_line ADD COLUMN IF NOT EXISTS orig_bill_no text;
-- One row per ERP sale/return line
CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_line_erp_line
    ON sales.sales_line (source, bill_no, inventory_ref_code) WHERE inventory_ref_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_sales_line_source_date ON sales.sales_line (source, sale_date);
CREATE INDEX IF NOT EXISTS ix_sales_line_barcode     ON sales.sales_line (barcode);
CREATE INDEX IF NOT EXISTS ix_sales_line_inv_code    ON sales.sales_line (item_inv_own_code);

-- The sales to analyse: ERP rows wherever the ERP has been loaded, legacy CSV
-- rows only for dates before the first successfully loaded ERP date.
CREATE OR REPLACE VIEW sales.v_sales_line AS
WITH erp_start AS (
    SELECT min(date_from) AS d FROM sales.load_log WHERE source = 'erp' AND status = 'success'
)
SELECT s.*
FROM sales.sales_line s, erp_start
WHERE s.source = 'erp'
   OR (s.source = 'csv_legacy' AND (erp_start.d IS NULL OR s.sale_date < erp_start.d));

GRANT USAGE ON SCHEMA sales TO powerbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO powerbi_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA sales
    GRANT SELECT ON TABLES TO powerbi_reader;

RESET ROLE;

-- Five years of sales, ALL product groups (diamond, gold, silver), from the ERP's Criteria-wise Sales
-- Search exports per financial year (2021-22 onwards). Used for seasonality only: label value
-- (no taxable amount), and the exports leave out sales of order-booked pieces. Loaded by
-- python -m kkj.pipelines.history_import; each file only after its rows and sums match its totals row.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS sales.hist_line (
    source_file      text NOT NULL,
    sr_no            int NOT NULL,
    sale_date        date NOT NULL,
    bill_no          text,
    label_no         text,
    barcode          text,
    item_name        text,
    category         text,
    sub_category     text,
    item_class_group text,
    location         text,
    purity           numeric,
    pcs              numeric,
    gross_wt         numeric,
    net_wt           numeric,
    dia_wt           numeric,
    item_amt         numeric,
    supplier         text,
    age_days         numeric,
    order_no         text,
    PRIMARY KEY (source_file, sr_no)
);
CREATE INDEX IF NOT EXISTS hist_line_date ON sales.hist_line (sale_date);
GRANT SELECT ON sales.hist_line TO powerbi_reader, dashboard_app;
RESET ROLE;
