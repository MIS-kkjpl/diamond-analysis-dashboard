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

-- Ask Stockwise conversations: a question can follow up on earlier ones in the same conversation
CREATE TABLE IF NOT EXISTS ai.conversation (
    conversation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    started_by      text,
    title           text
);
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS conversation_id bigint REFERENCES ai.conversation (conversation_id);
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS page text;                        -- what the person was looking at
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'done';  -- running, done, failed
CREATE INDEX IF NOT EXISTS ix_question_conversation ON ai.question (conversation_id, question_id);

-- Conversations belong to an account (not a display name); live progress while an answer is written
ALTER TABLE ai.conversation ADD COLUMN IF NOT EXISTS user_id int;
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS user_id int;
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS progress jsonb;        -- the lookups done so far, as shown live
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS partial_md text;       -- the answer as it is being written
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS proposed_notes jsonb;  -- business notes the AI offers to save
ALTER TABLE ai.question ADD COLUMN IF NOT EXISTS thinking text;        -- its reasoning summary, shown live, cleared after
UPDATE ai.conversation v SET user_id = u.user_id FROM auth.app_user u WHERE v.user_id IS NULL AND u.display_name = v.started_by;
UPDATE ai.question q SET user_id = u.user_id FROM auth.app_user u WHERE q.user_id IS NULL AND u.display_name = q.asked_by;
CREATE INDEX IF NOT EXISTS ix_conversation_user ON ai.conversation (user_id, conversation_id DESC);

GRANT USAGE ON SCHEMA ai TO dashboard_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ai TO dashboard_app;
GRANT UPDATE ON ai.question TO dashboard_app;     -- the answer is written after the question
GRANT DELETE ON ai.question, ai.conversation TO dashboard_app;   -- people delete their own chats (the cost log stays)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ai TO dashboard_app;

RESET ROLE;
