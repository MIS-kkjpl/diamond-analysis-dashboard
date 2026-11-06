-- Schema auth: Stockwise sign-in. Accounts, sessions and a log of sign-in attempts.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- Three roles (kkj.web.roles): admin (everything, including accounts and settings), manager (acts on
-- recommendations, writes AI briefs and notes), viewer (sees everything, changes nothing).
-- Accounts: Settings -> Users (admins), or python -m kkj.web.users.
-- powerbi_reader gets nothing here.

CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION erp_etl;
SET ROLE erp_etl;

CREATE TABLE IF NOT EXISTS auth.app_user (
    user_id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username             text NOT NULL,
    display_name         text NOT NULL,             -- recorded with approvals, notes and settings changes
    password_hash        text NOT NULL,             -- Argon2id, encoded with its own salt and parameters
    active               boolean NOT NULL DEFAULT true,
    failed_logins        integer NOT NULL DEFAULT 0,   -- consecutive; reset by a successful sign-in
    locked_until         timestamptz,
    last_login_at        timestamptz,
    password_changed_at  timestamptz NOT NULL DEFAULT now(),
    created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS app_user_username ON auth.app_user (lower(username));

-- The browser holds a random token; only its SHA-256 is stored, so a copy of this table can't sign anyone in
CREATE TABLE IF NOT EXISTS auth.session (
    token_hash    text PRIMARY KEY,
    user_id       integer NOT NULL REFERENCES auth.app_user ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,            -- absolute limit; the idle limit is checked against last_seen_at
    ip            text,
    user_agent    text
);
CREATE INDEX IF NOT EXISTS session_by_user ON auth.session (user_id);

CREATE TABLE IF NOT EXISTS auth.login_event (
    event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    username   text NOT NULL,                      -- as typed
    user_id    integer,
    success    boolean NOT NULL,
    reason     text,                               -- bad_password, unknown_user, locked, inactive, password_changed
    ip         text
);

-- Added after the first version: a password set by someone else is temporary and must be changed at sign-in
ALTER TABLE auth.app_user ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;

-- Roles, added after: existing accounts were all-powerful, so they start as admin; new accounts default to manager
ALTER TABLE auth.app_user ADD COLUMN IF NOT EXISTS role text;
UPDATE auth.app_user SET role = 'admin' WHERE role IS NULL;
ALTER TABLE auth.app_user ALTER COLUMN role SET DEFAULT 'manager';
ALTER TABLE auth.app_user ALTER COLUMN role SET NOT NULL;
ALTER TABLE auth.app_user DROP CONSTRAINT IF EXISTS app_user_role_check;
ALTER TABLE auth.app_user ADD CONSTRAINT app_user_role_check CHECK (role IN ('admin', 'manager', 'viewer'));

-- Admins manage accounts from Settings -> Users, so the app may add and edit them (the app checks the role)
GRANT USAGE ON SCHEMA auth TO dashboard_app;
GRANT SELECT ON auth.app_user TO dashboard_app;
GRANT INSERT (username, display_name, password_hash, must_change_password, role) ON auth.app_user TO dashboard_app;
GRANT UPDATE (failed_logins, locked_until, last_login_at, password_hash, password_changed_at,
              display_name, active, must_change_password, role)
    ON auth.app_user TO dashboard_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.session TO dashboard_app;
GRANT SELECT, INSERT ON auth.login_event TO dashboard_app;

RESET ROLE;
