-- Schema ai: what the AI features (kkj.ai) produced, and what each call cost.
-- Run by kkj.db.setup as a superuser connected to kkj_erp_stock. Safe to re-run.
-- Only the dashboard account reads and writes it (not Power BI): questions people ask stay inside the app.

CREATE SCHEMA IF NOT EXISTS ai AUTHORIZATION erp_etl;
SET ROLE erp_etl;

-- Every call to the Claude API: who, for which feature, tokens and the estimated cost
CREATE TABLE IF NOT EXISTS ai.call_log (
    call_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    feature        text NOT NULL,                  -- ai_brief, ai_ask, ai_review, ai_season
    asked_by       text,
    model          text,
    input_tokens   int, output_tokens int, cache_read_tokens int, cache_write_tokens int,
    cost_usd       numeric(10, 4),
    seconds        numeric(8, 2),
    ok             boolean NOT NULL DEFAULT true,
    error          text
);

-- Generated texts: the weekly brief, season notes. facts = the computed figures they were written from
CREATE TABLE IF NOT EXISTS ai.text (
    text_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('brief', 'season')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text,
    period      text,                              -- e.g. 'week to 06 Nov 2026'
    body_md     text NOT NULL,
    facts       jsonb,
    call_id     bigint REFERENCES ai.call_log (call_id)
);
CREATE INDEX IF NOT EXISTS ix_text_kind ON ai.text (kind, created_at DESC);

-- Ask Stockwise: questions and answers, with the data lookups behind each answer
CREATE TABLE IF NOT EXISTS ai.question (
    question_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asked_at    timestamptz NOT NULL DEFAULT now(),
    asked_by    text,
    question    text NOT NULL,
    answer_md   text,
    lookups     jsonb,                             -- [{tool, input}] in the order they ran
    call_id     bigint REFERENCES ai.call_log (call_id)
);

-- The AI second look at the recommendation list: one note per finding, linked to the cards it is about
CREATE TABLE IF NOT EXISTS ai.review_note (
    note_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_at   timestamptz NOT NULL DEFAULT now(),
    review_id   bigint NOT NULL,                   -- one review = one call; the latest review is shown
    note_type   text NOT NULL,                     -- conflict, timing, duplicate, caution, data
    note        text NOT NULL,
    action_ids  bigint[] NOT NULL,
    call_id     bigint REFERENCES ai.call_log (call_id)
);
CREATE INDEX IF NOT EXISTS ix_review_note_review ON ai.review_note (review_id);

GRANT USAGE ON SCHEMA ai TO dashboard_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ai TO dashboard_app;
GRANT UPDATE ON ai.question TO dashboard_app;     -- the answer is written after the question
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ai TO dashboard_app;

RESET ROLE;
