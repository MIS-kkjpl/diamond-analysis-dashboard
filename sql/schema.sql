-- Schema for the Item-Wise Current Stock pipeline (database kkj_erp_stock).
-- Run as a superuser connected to kkj_erp_stock; db_setup.py does this.
-- Safe to re-run. Roles erp_etl / powerbi_reader are created by db_setup.py
-- (their passwords live in .env only, never in this file).

CREATE SCHEMA IF NOT EXISTS erp AUTHORIZATION erp_etl;

-- Objects below are created AS erp_etl, so the pipeline owns them (full rights on erp only)
SET ROLE erp_etl;

CREATE TABLE IF NOT EXISTS erp.etl_run_log (
    run_id        serial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    source        text NOT NULL CHECK (source IN ('erp', 'file')),
    report_id     text,
    raw_file      text,
    record_count  int,
    rows_loaded   int,
    validation    jsonb,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'success', 'failed')),
    error         text,
    dq_warnings   jsonb      -- data-quality warnings with counts (never block the load)
);
-- For databases created before dq_warnings existed
ALTER TABLE erp.etl_run_log ADD COLUMN IF NOT EXISTS dq_warnings jsonb;

-- One row per label per snapshot day
CREATE TABLE IF NOT EXISTS erp.stock_snapshot (
    snapshot_date        date          NOT NULL,
    sr_no                int           NOT NULL,
    label_no             int,
    category             text,
    item_class_group     text,
    branch               text,
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
    age_days             int,
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
    report_id            text,
    run_id               int REFERENCES erp.etl_run_log (run_id),
    loaded_at            timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, barcode)   -- barcode verified unique in the 1328 export
);

CREATE INDEX IF NOT EXISTS ix_stock_snapshot_branch           ON erp.stock_snapshot (branch);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_category         ON erp.stock_snapshot (category);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_item_class_group ON erp.stock_snapshot (item_class_group);
-- (snapshot_date) needs no separate index: it leads the primary key.

-- ── 50K price slabs (used by the views; one definition, also used by tests) ─
-- Slabs of 50,000 up to 50 lakh, upper bound inclusive:
--   <= 50,000 -> '0-50K' (sort 0), 50,000 < amt <= 1,00,000 -> '50K-1L' (1), ...
--   49.5L < amt <= 50L -> '49.5L-50L' (99), > 50L -> '50L+' (100), NULL -> 'Unknown' (999)

-- Slab number: CEIL(amt / 50000) - 1, with 0 (and below) in slab 0 and > 50L in 100
CREATE OR REPLACE FUNCTION erp.price_slab_sort(amt numeric) RETURNS int
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
               WHEN amt IS NULL    THEN 999
               WHEN amt > 5000000  THEN 100
               ELSE GREATEST(CEIL(amt / 50000)::int - 1, 0)
           END
$$;

-- Short Indian amount: 0 -> '0', 50000 -> '50K', 100000 -> '1L', 150000 -> '1.5L'
CREATE OR REPLACE FUNCTION erp.inr_short(amount numeric) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
               WHEN amount = 0       THEN '0'
               WHEN amount < 100000  THEN trim_scale(amount / 1000)::text || 'K'
               ELSE trim_scale(amount / 100000)::text || 'L'
           END
$$;

CREATE OR REPLACE FUNCTION erp.price_slab(amt numeric) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE erp.price_slab_sort(amt)
               WHEN 999 THEN 'Unknown'
               WHEN 100 THEN '50L+'
               ELSE erp.inr_short(erp.price_slab_sort(amt) * 50000::numeric) || '-'
                 || erp.inr_short((erp.price_slab_sort(amt) + 1) * 50000::numeric)
           END
$$;

-- Lower bound of the slab (for range filters); 50L+ starts at 50,00,000
CREATE OR REPLACE FUNCTION erp.price_slab_from(amt numeric) RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE erp.price_slab_sort(amt)
               WHEN 999 THEN NULL
               ELSE erp.price_slab_sort(amt) * 50000::numeric
           END
$$;

-- ── Shared derived-column logic (used by v_stock_latest and v_stock_history) ─
-- Age bucket of a number of days: 0-30, 31-90, 91-180, 181-365, 365+
CREATE OR REPLACE FUNCTION erp.age_bucket(days int) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
               WHEN days <= 30  THEN '0-30'
               WHEN days <= 90  THEN '31-90'
               WHEN days <= 180 THEN '91-180'
               WHEN days <= 365 THEN '181-365'
               ELSE '365+'
           END
$$;

-- "Sort by column" for erp.age_bucket: 1..5
CREATE OR REPLACE FUNCTION erp.age_bucket_sort(days int) RETURNS int
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
               WHEN days <= 30  THEN 1
               WHEN days <= 90  THEN 2
               WHEN days <= 180 THEN 3
               WHEN days <= 365 THEN 4
               ELSE 5
           END
$$;

-- "KJ2 / Branch Dharampeth" -> "KJ2"; names without " / " stay as they are
CREATE OR REPLACE FUNCTION erp.branch_short(branch text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
               WHEN branch LIKE '% / %' THEN split_part(branch, ' / ', 1)
               ELSE branch
           END
$$;

-- ── Views for Power BI ──────────────────────────────────────────────────────
-- CREATE OR REPLACE can only ADD columns at the end of a view; to change or
-- remove a column, DROP VIEW first.

-- Latest snapshot day, every column, plus Power BI helper columns
CREATE OR REPLACE VIEW erp.v_stock_latest AS
SELECT s.*,
       erp.age_bucket(s.age_days)      AS ageing_bucket,
       erp.age_bucket_sort(s.age_days) AS ageing_sort,     -- "Sort by column" for ageing_bucket
       CASE                                     -- purity ranges; sort order in karat_sort
           WHEN s.purity = 0   THEN 'No Metal'
           WHEN s.purity >= 99 THEN '24K'
           WHEN s.purity >= 91 THEN '22K'
           WHEN s.purity >= 83 THEN '20K'
           WHEN s.purity >= 75 THEN '18K'
           WHEN s.purity >= 58 THEN '14K'
           WHEN s.purity >= 37 THEN '9K'
           ELSE 'Other'
       END AS karat,
       erp.branch_short(s.branch) AS branch_short,
       -- New columns go at the end (CREATE OR REPLACE VIEW can only append)
       CASE                                     -- "Sort by column" for karat
           WHEN s.purity = 0   THEN 7
           WHEN s.purity >= 99 THEN 1
           WHEN s.purity >= 91 THEN 2
           WHEN s.purity >= 83 THEN 3
           WHEN s.purity >= 75 THEN 4
           WHEN s.purity >= 58 THEN 5
           WHEN s.purity >= 37 THEN 6
           ELSE 8
       END AS karat_sort,
       (s.item_class_group = 'Customer Diamond') AS is_customer_item,
       erp.age_bucket(s.days_from_branch_in)      AS age_bucket_branch,  -- ageing since branch-in
       erp.age_bucket_sort(s.days_from_branch_in) AS age_bucket_branch_sort,
       COALESCE(s.labeling_date, s.label_dt) AS labeling_date_final, -- use for date filters
       CASE                                     -- price band of the label's item_amt
           WHEN s.item_amt < 50000  THEN '< 50K'
           WHEN s.item_amt < 100000 THEN '50K-1L'
           WHEN s.item_amt < 300000 THEN '1L-3L'
           WHEN s.item_amt < 500000 THEN '3L-5L'
           ELSE '5L+'
       END AS price_band,
       CASE                                     -- "Sort by column" for price_band
           WHEN s.item_amt < 50000  THEN 1
           WHEN s.item_amt < 100000 THEN 2
           WHEN s.item_amt < 300000 THEN 3
           WHEN s.item_amt < 500000 THEN 4
           ELSE 5
       END AS price_band_sort,
       to_char(COALESCE(s.labeling_date, s.label_dt), 'YYYY-MM') AS labeling_month,
       (extract(year FROM COALESCE(s.labeling_date, s.label_dt)) * 100
        + extract(month FROM COALESCE(s.labeling_date, s.label_dt)))::int AS labeling_month_sort,
       extract(year FROM COALESCE(s.labeling_date, s.label_dt))::int AS labeling_year,
       erp.price_slab(s.item_amt)      AS price_slab,
       erp.price_slab_sort(s.item_amt) AS price_slab_sort,
       erp.price_slab_from(s.item_amt) AS price_slab_from
FROM erp.stock_snapshot s
WHERE s.snapshot_date = (SELECT max(snapshot_date) FROM erp.stock_snapshot);

-- Every snapshot day, with the derived columns the Overview page slices by, so one
-- slicer filters "today" and the comparison day the same way.
-- is_on_approval: location contains "approval" (Approval, HO/KJ2/KJ3/KJ4/KJ7 Approval).
CREATE OR REPLACE VIEW erp.v_stock_history AS
SELECT s.*,
       erp.branch_short(s.branch)                 AS branch_short,
       erp.age_bucket(s.age_days)                 AS ageing_bucket,
       erp.age_bucket_sort(s.age_days)            AS ageing_sort,
       erp.age_bucket(s.days_from_branch_in)      AS age_bucket_branch,
       erp.age_bucket_sort(s.days_from_branch_in) AS age_bucket_branch_sort,
       erp.price_slab(s.item_amt)                 AS price_slab,
       erp.price_slab_sort(s.item_amt)            AS price_slab_sort,
       erp.price_slab_from(s.item_amt)            AS price_slab_from,
       (s.item_class_group = 'Customer Diamond')  AS is_customer_item,
       COALESCE(s.labeling_date, s.label_dt)      AS labeling_date_final,
       (s.location ILIKE '%approval%')            AS is_on_approval
FROM erp.stock_snapshot s;

-- Daily totals per branch, item group and price slab, for trends without loading
-- every row. (Slab columns are last: CREATE OR REPLACE VIEW can only append.)
CREATE OR REPLACE VIEW erp.v_daily_branch_summary AS
SELECT snapshot_date,
       branch,
       item_class_group,
       count(*)      AS labels,
       sum(pcs)      AS pcs,
       sum(gross_wt) AS gross_wt,
       sum(net_wt)   AS net_wt,
       sum(dia_wt)   AS dia_wt,
       sum(item_amt) AS item_amt,
       erp.price_slab(item_amt)      AS price_slab,
       erp.price_slab_sort(item_amt) AS price_slab_sort
FROM erp.stock_snapshot
GROUP BY snapshot_date, branch, item_class_group,
         erp.price_slab(item_amt), erp.price_slab_sort(item_amt);

-- Latest successful pipeline run, for a "Data as of" card.
-- finished_at_ist: the same moment as plain India time (Power BI shows it as-is).
CREATE OR REPLACE VIEW erp.v_last_run AS
SELECT run_id,
       finished_at,
       record_count,
       report_id,
       (finished_at AT TIME ZONE 'Asia/Kolkata') AS finished_at_ist
FROM erp.etl_run_log
WHERE status = 'success'
ORDER BY finished_at DESC
LIMIT 1;

-- Power BI: read-only on schema erp, now and for future tables/views
-- ("ALL TABLES" includes views: v_stock_latest, v_daily_branch_summary, v_last_run)
GRANT USAGE ON SCHEMA erp TO powerbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA erp TO powerbi_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA erp
    GRANT SELECT ON TABLES TO powerbi_reader;

RESET ROLE;
