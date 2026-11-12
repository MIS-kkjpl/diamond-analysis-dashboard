-- What happens to each label in the ERP, and what each location and voucher type means.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
--
--   erp.label_voucher        every voucher on a label's ERP history (Label Status screen, kkj.erp.label_status),
--                            per financial-year company; fetched for labels that enter or leave stock
--   erp.label_voucher_fetch  when each label's history was last fetched, per financial year
--   config.location          every stock location: its kind and whether pieces there count as stock
--   config.voucher_type      every voucher type / document series seen: what it does to stock

SET ROLE erp_etl;

CREATE TABLE IF NOT EXISTS erp.label_voucher (
    barcode      text NOT NULL,
    fy           text NOT NULL,                 -- financial-year company, e.g. '2026-27'
    seq          int  NOT NULL,                 -- order in the ERP's own list
    doc_date     date,
    doc_no       text,                          -- e.g. MHODDJ-288
    series       text,                          -- doc_no without the number, e.g. MHODDJ
    voucher      text,                          -- Labeling, Stock Transfer, Sale, BookedOut, Stock Outward, ...
    tx_type      text,                          -- In / Out
    pieces       numeric, pieces_out numeric,
    location     text,
    order_no     text,
    tag          text,
    cancelled    boolean,
    PRIMARY KEY (barcode, fy, seq)
);
CREATE INDEX IF NOT EXISTS label_voucher_doc ON erp.label_voucher (doc_date, voucher);

CREATE TABLE IF NOT EXISTS erp.label_voucher_fetch (
    barcode      text NOT NULL,
    fy           text NOT NULL,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    vouchers     int,
    error        text,
    PRIMARY KEY (barcode, fy)
);
GRANT SELECT ON erp.label_voucher, erp.label_voucher_fetch TO powerbi_reader;
RESET ROLE;
GRANT USAGE ON SCHEMA erp TO dashboard_app;
GRANT SELECT ON erp.label_voucher, erp.label_voucher_fetch TO dashboard_app;

SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS config.location (
    location         text PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'unclassified'
                     CHECK (kind IN ('counter', 'approval', 'in_transit', 'repair', 'storage', 'dispatch', 'supply',
                                     'out', 'unclassified')),
    counts_as_stock  boolean NOT NULL DEFAULT true,
    note             text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text
);
GRANT SELECT ON config.location TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON config.location TO dashboard_app;

CREATE TABLE IF NOT EXISTS config.voucher_type (
    voucher      text NOT NULL,                 -- as the ERP names it
    series       text NOT NULL DEFAULT '*',     -- a document series, or '*' for any series of this voucher
    effect       text NOT NULL DEFAULT 'unknown'
                 CHECK (effect IN ('sale', 'sale_return', 'booking', 'booking_return', 'outward', 'inward', 'labelling',
                                   'transfer', 'repair_issue', 'repair_receipt', 'approval_issue', 'approval_return',
                                   'purchase', 'purchase_return', 'set_combine', 'set_split', 'relabel',
                                   'no_effect', 'other', 'unknown')),
    note         text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    PRIMARY KEY (voucher, series)
);
GRANT SELECT ON config.voucher_type TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON config.voucher_type TO dashboard_app;
RESET ROLE;

SET ROLE erp_etl;
-- effects added after the first version
ALTER TABLE config.voucher_type DROP CONSTRAINT IF EXISTS voucher_type_effect_check;
ALTER TABLE config.voucher_type ADD CONSTRAINT voucher_type_effect_check CHECK (effect IN (
    'sale', 'sale_return', 'booking', 'booking_return', 'outward', 'inward', 'labelling', 'transfer', 'repair_issue',
    'repair_receipt', 'approval_issue', 'approval_return', 'purchase', 'purchase_return', 'set_combine', 'set_split',
    'relabel', 'no_effect', 'other', 'unknown'));
RESET ROLE;

-- Seed: only rows not there yet, so edits made in Settings are kept.
INSERT INTO config.location (location, kind, counts_as_stock, note, updated_by) VALUES
    ('HO MD COUNTER', 'out', false, 'Pieces leaving by Stock Outward: damaged piece or tag (MHODDJ), remaining piece of a broken set (MHODB); relabelled later under a new barcode', 'setup'),
    ('HO Repair', 'repair', true, NULL, 'setup'),
    ('KJ2 Repairing Counter', 'repair', true, NULL, 'setup'),
    ('MO Gold Rep', 'approval', true, 'Mumbai: pieces out for repair that come back under the same tag (unlike HO MD COUNTER); still our stock', 'setup'),
    ('HO Safe', 'storage', true, NULL, 'setup'),
    ('HO Vault', 'storage', true, NULL, 'setup'),
    ('KJ2 Safe', 'storage', true, NULL, 'setup'),
    ('HO Parcel', 'dispatch', true, 'Packed for dispatch, before HO In Transit', 'setup'),
    ('Supply Counter', 'supply', true, 'HO dispatch point: pieces arrive after labelling and leave for branches / exhibitions; sets are made here', 'setup'),
    ('RJ Supply Counter', 'supply', true, 'Dispatch point for Rajasthan', 'setup'),
    ('MO Supply Counter', 'supply', true, 'Dispatch point for Mumbai', 'setup')
ON CONFLICT (location) DO NOTHING;

INSERT INTO config.voucher_type (voucher, series, effect, note, updated_by) VALUES
    ('Stock Outward', 'MHODDJ', 'outward', 'Damaged piece or tag, from HO MD COUNTER (FY 2026-27)', 'setup'),
    ('Stock Outward', 'LHODDJ', 'outward', 'Damaged piece or tag, from HO MD COUNTER (FY 2025-26)', 'setup'),
    ('Stock Outward', 'MHODB', 'outward', 'Remaining piece of a broken set, from HO MD COUNTER (FY 2026-27)', 'setup'),
    ('Stock Outward', 'LHODB', 'outward', 'Remaining piece of a broken set, from HO MD COUNTER (FY 2025-26)', 'setup'),
    ('Sale', '*', 'sale', NULL, 'setup'),
    ('BookedOut', '*', 'booking', 'Reserved for a customer order; the stock report leaves it out until sold or released', 'setup'),
    ('BookedIn', '*', 'booking_return', 'Booking released back to stock', 'setup'),
    ('Labeling', '*', 'labelling', NULL, 'setup'),
    ('Stock Transfer', '*', 'transfer', NULL, 'setup'),
    ('Goldsmith Issue', '*', 'repair_issue', NULL, 'setup'),
    ('Stock Outward', 'LHOSC', 'set_combine', 'Pieces combined into a set (a set label comes in, SetIn); to confirm', 'setup'),
    ('Stock Outward', 'MHOSC', 'set_combine', 'Pieces combined into a set (a set label comes in, SetIn); to confirm', 'setup'),
    ('SetIn', '*', 'set_combine', 'The set label made from combined pieces; to confirm', 'setup'),
    ('Set Out', '*', 'set_split', 'A set broken up: the set label goes out, its pieces come in (StkIn, same document); to confirm', 'setup'),
    ('Reprinting', '*', 'no_effect', 'Tag reprinted', 'setup')
ON CONFLICT (voucher, series) DO NOTHING;


-- every other location seen in stock, classified from its name; anything unclear stays 'unclassified' (the audit
-- fails until someone classifies it in Settings -> Locations)
INSERT INTO config.location (location, kind, updated_by)
SELECT location,
       CASE WHEN location ILIKE '%approval%' THEN 'approval'
            WHEN location ~* 'trans' THEN 'in_transit'
            WHEN location ILIKE '%repair%' THEN 'repair'
            WHEN location ~* 'z counter' THEN 'counter'   -- temporary promotional counters
            WHEN location ~* 'counter|branch|mo diamond|exhibition' THEN 'counter'
            ELSE 'unclassified' END,
       'setup'
FROM (SELECT DISTINCT location FROM erp.stock_snapshot UNION SELECT DISTINCT location FROM stock_hist.snapshot) s
WHERE location IS NOT NULL
ON CONFLICT (location) DO NOTHING;

-- Customer-order labels (stock report with "Show Order Labels: Only"), one set per day. The normal stock pull leaves
-- them out, so erp.stock_snapshot stays the available stock; this records what is held for customer orders.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS erp.order_snapshot (LIKE erp.stock_snapshot INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
GRANT SELECT ON erp.order_snapshot TO powerbi_reader;
RESET ROLE;
GRANT SELECT ON erp.order_snapshot TO dashboard_app;
