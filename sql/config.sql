-- Schema config: business rules and reference lists edited from the dashboard.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- The dashboard writes here as dashboard_app (and ONLY here); everything else it reads.

CREATE SCHEMA IF NOT EXISTS config AUTHORIZATION erp_etl;
SET ROLE erp_etl;

-- One row per business rule. value is JSON so a rule can be a number, a flag or a list.
CREATE TABLE IF NOT EXISTS config.setting (
    key          text PRIMARY KEY,
    value        jsonb NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text
);

-- Salespeople as the ERP writes them on sales lines ("57 Deepak Dholke" -> sales_code K0057)
CREATE TABLE IF NOT EXISTS config.salesperson (
    sales_code     text PRIMARY KEY,          -- normalised: K + at least 4 digits, upper case
    name           text NOT NULL,
    employee_code  text,                      -- HR code, when known
    branch         text,
    kind           text NOT NULL DEFAULT 'salesperson'
                   CHECK (kind IN ('salesperson', 'owner', 'generic')),
    active         boolean NOT NULL DEFAULT true,
    left_on        date,
    match_status   text NOT NULL DEFAULT 'unmapped'
                   CHECK (match_status IN ('matched', 'name_match', 'manual', 'unmapped')),
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text
);

-- Every change made from the app: what, when, by whom (the signed-in account's display name)
CREATE TABLE IF NOT EXISTS config.change_log (
    change_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   text NOT NULL,
    table_name   text NOT NULL,
    row_key      text NOT NULL,
    old_value    jsonb,
    new_value    jsonb,
    note         text
);

GRANT USAGE ON SCHEMA config TO powerbi_reader, dashboard_app;
GRANT SELECT ON ALL TABLES IN SCHEMA config TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON config.setting, config.salesperson, config.change_log TO dashboard_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA config GRANT SELECT ON TABLES TO powerbi_reader, dashboard_app;

-- dashboard_app reads every other schema, writes none of them
GRANT USAGE ON SCHEMA erp, sales, stock_hist TO dashboard_app;
GRANT SELECT ON ALL TABLES IN SCHEMA erp, sales, stock_hist TO dashboard_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_etl IN SCHEMA erp, sales, stock_hist GRANT SELECT ON TABLES TO dashboard_app;

RESET ROLE;

-- Branches as the ERP names them, with the short codes people use (added after the first version)
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS config.branch (
    erp_branch   text PRIMARY KEY,                 -- e.g. 'KJ2 / Branch Dharampeth'
    short_name   text NOT NULL,                    -- e.g. 'KJ2'
    hr_code      text,                             -- code in the HR/user list, e.g. 'DP'
    place        text,                             -- e.g. 'Dharampeth'
    kind         text NOT NULL DEFAULT 'showroom'
                 CHECK (kind IN ('showroom', 'head_office', 'wholesale', 'exhibition', 'other')),
    active       boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text
);
GRANT SELECT ON config.branch TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE ON config.branch TO dashboard_app;
RESET ROLE;

-- Ideal stock turn per brick, brick group, product or item group (added after the first version).
-- The most specific row wins; anything without one uses the target_turn rule. Edited in Settings -> Ideal turns.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS config.ideal_turn (
    level       text NOT NULL CHECK (level IN ('brick', 'group', 'product', 'item_group')),
    key         text NOT NULL,                 -- e.g. '18KT · D RING · 0.25–0.5ct' for a group, 'D RING' for a product
    ideal       numeric(6,2) NOT NULL CHECK (ideal > 0),
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    PRIMARY KEY (level, key)
);
GRANT SELECT ON config.ideal_turn TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE, DELETE ON config.ideal_turn TO dashboard_app;
RESET ROLE;

-- Festival dates (added after the first version): the jewellery-buying days seasonality is aligned to,
-- because they move between months year to year. Edited in Settings -> Festivals.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS config.festival (
    name        text NOT NULL,              -- e.g. 'Dhanteras'
    day         date NOT NULL,
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    PRIMARY KEY (name, day)
);
GRANT SELECT ON config.festival TO powerbi_reader, dashboard_app;
GRANT INSERT, UPDATE, DELETE ON config.festival TO dashboard_app;
RESET ROLE;

-- Business notes: facts about the business that every AI feature reads (kkj.ai.notes). Shared by everyone;
-- each note records who added it; only admins (STOCKWISE_ADMINS in .env) can edit or remove one.
SET ROLE erp_etl;
CREATE TABLE IF NOT EXISTS config.business_note (
    note_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note        text NOT NULL,
    added_by    text NOT NULL,
    added_at    timestamptz NOT NULL DEFAULT now(),
    source      text NOT NULL DEFAULT 'settings' CHECK (source IN ('settings', 'chat')),
    active      boolean NOT NULL DEFAULT true,
    updated_by  text,
    updated_at  timestamptz
);
GRANT SELECT ON config.business_note TO dashboard_app;
GRANT INSERT, UPDATE ON config.business_note TO dashboard_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA config TO dashboard_app;
RESET ROLE;
