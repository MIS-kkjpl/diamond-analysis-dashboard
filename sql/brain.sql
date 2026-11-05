-- Schema brain: the decisions the system proposes and what happens to them.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- Derived data (rebuilt by kkj.brain.run from erp/sales/stock_hist/config), so the
-- dashboard account may write it; it never writes the source schemas.

CREATE SCHEMA IF NOT EXISTS brain AUTHORIZATION erp_etl;
SET ROLE erp_etl;

-- One row per refresh of the decisions
CREATE TABLE IF NOT EXISTS brain.run_log (
    run_id        serial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    trigger       text NOT NULL,                 -- 'pipeline', 'dashboard', 'manual'
    stock_as_of   date,
    sales_to      date,
    candidates    int,
    created       int,
    updated       int,
    resolved      int,
    reopened      int,
    status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    error         text
);

-- One row per decision; action_key is stable across refreshes ("transfer|<group>|KJ5|KJ2")
CREATE TABLE IF NOT EXISTS brain.action (
    action_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_key     text NOT NULL UNIQUE,
    kind           text NOT NULL,
    title          text NOT NULL,
    summary        text,
    branch         text,
    subject        jsonb,                        -- what it is about (group, supplier, salesperson ...)
    impact_value   numeric(16,2),                -- ₹ the decision moves, frees or protects
    impact_label   text,
    priority       numeric(10,4) NOT NULL DEFAULT 0,
    confidence     text CHECK (confidence IN ('high', 'medium', 'low')),
    evidence       jsonb,                        -- the numbers behind it
    pieces         jsonb,                        -- the labels involved, if any
    status         text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'approved', 'rejected', 'snoozed', 'done', 'resolved')),
    snoozed_until  date,
    decided_at     timestamptz,
    decided_by     text,
    decision_note  text,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    first_run_id   int REFERENCES brain.run_log (run_id),
    last_run_id    int REFERENCES brain.run_log (run_id),
    times_seen     int NOT NULL DEFAULT 1,
    first_impact   numeric(16,2)                 -- impact when first proposed, to show progress
);
CREATE INDEX IF NOT EXISTS ix_action_status_kind ON brain.action (status, kind);

-- Everything that happens to a decision
CREATE TABLE IF NOT EXISTS brain.action_event (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id   bigint NOT NULL REFERENCES brain.action (action_id),
    at          timestamptz NOT NULL DEFAULT now(),
    by          text NOT NULL,                   -- a person's name, or 'system'
    event       text NOT NULL,                   -- created, approved, rejected, snoozed, done, reopened,
                                                 -- auto_resolved, condition_cleared, snooze_ended, comment
    note        text,
    data        jsonb
);
CREATE INDEX IF NOT EXISTS ix_action_event_action ON brain.action_event (action_id, at);

-- Weighted stock turn per brick (kkj.metrics.brick_turn), one set per stock date; scope = 'Company' or a branch.
-- Added after the first version. Turns may be 'Infinity' (sold with no stock held).
CREATE TABLE IF NOT EXISTS brain.brick_turn (
    as_of            date NOT NULL,
    scope            text NOT NULL,
    brick            text NOT NULL,
    run_id           int REFERENCES brain.run_log (run_id),
    item_class_group text, purity text, item_name text, category text, sub_category text,
    weight_band      text, band_order int,
    current_pcs      numeric, current_value numeric, avg_piece_value numeric,
    data_3m int, avail_3m int, avg_pcs_3m double precision, sold_3m numeric, annual_3m double precision,
    turn_3m double precision, q_3m boolean,
    data_6m int, avail_6m int, avg_pcs_6m double precision, sold_6m numeric, annual_6m double precision,
    turn_6m double precision, q_6m boolean,
    data_12m int, avail_12m int, avg_pcs_12m double precision, sold_12m numeric, annual_12m double precision,
    turn_12m double precision, q_12m boolean,
    pattern          text,                         -- qualifying windows, e.g. '3m+6m+12m'
    weights          text,                         -- e.g. '60/30/10'
    blended_turn     double precision,
    annual_sales     double precision,             -- blended, pcs a year
    ideal_turn       double precision,
    ideal_pcs        numeric,
    change_pcs       numeric,
    status           text NOT NULL,                -- increase, reduce, keep, new, hold, phase_out, stock_up, no_history
    PRIMARY KEY (as_of, scope, brick)
);

-- Added after: rows for brick groups too (level), each brick's group, and where its ideal turn was set
ALTER TABLE brain.brick_turn ADD COLUMN IF NOT EXISTS level text NOT NULL DEFAULT 'brick';
ALTER TABLE brain.brick_turn ADD COLUMN IF NOT EXISTS brick_group text;
ALTER TABLE brain.brick_turn ADD COLUMN IF NOT EXISTS ideal_source text;
ALTER TABLE brain.brick_turn DROP CONSTRAINT IF EXISTS brick_turn_pkey;
ALTER TABLE brain.brick_turn ADD PRIMARY KEY (as_of, scope, level, brick);   -- brick = the brick or group key

GRANT USAGE ON SCHEMA brain TO powerbi_reader, dashboard_app;
GRANT SELECT ON ALL TABLES IN SCHEMA brain TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA brain TO dashboard_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA brain TO dashboard_app;
GRANT DELETE ON brain.brick_turn TO dashboard_app;      -- a refresh replaces that stock date's set
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA brain GRANT SELECT ON TABLES TO powerbi_reader, dashboard_app;

RESET ROLE;
