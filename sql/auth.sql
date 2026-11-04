-- Schema auth: Stockwise sign-in. Accounts, sessions and a log of sign-in attempts.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- No roles: every account sees and can do everything, including managing accounts
-- (Settings -> Users, or python -m kkj.web.users).
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

-- Any signed-in person can manage accounts from Settings -> Users (no roles), so the app may add and edit them
GRANT USAGE ON SCHEMA auth TO dashboard_app;
GRANT SELECT ON auth.app_user TO dashboard_app;
GRANT INSERT (username, display_name, password_hash, must_change_password) ON auth.app_user TO dashboard_app;
GRANT UPDATE (failed_logins, locked_until, last_login_at, password_hash, password_changed_at,
              display_name, active, must_change_password)
    ON auth.app_user TO dashboard_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.session TO dashboard_app;
GRANT SELECT, INSERT ON auth.login_event TO dashboard_app;

RESET ROLE;
