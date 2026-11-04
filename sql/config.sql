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

-- Every change made from the app: what, when, by whom (typed in; there are no logins yet)
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
