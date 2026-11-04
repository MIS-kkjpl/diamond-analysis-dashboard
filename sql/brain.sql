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

GRANT USAGE ON SCHEMA brain TO powerbi_reader, dashboard_app;
GRANT SELECT ON ALL TABLES IN SCHEMA brain TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA brain TO dashboard_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA brain TO dashboard_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA brain GRANT SELECT ON TABLES TO powerbi_reader, dashboard_app;

RESET ROLE;
