-- Phase 3 schema v1 (arch §2.1 as revised by the 2026-08-27 design
-- audit): person/fact/triple/action_log. Episodes wait for the embedder
-- dimension (002); reminders/tasks/ledger land in 003.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS person (
  id           text PRIMARY KEY,
  chat_id      bigint UNIQUE,
  stage        text NOT NULL DEFAULT 'none',
  consented_at date
);

CREATE TABLE IF NOT EXISTS fact (
  id               uuid PRIMARY KEY,
  legacy_id        text UNIQUE,
  subject          text NOT NULL REFERENCES person(id),
  subject_reviewed boolean NOT NULL DEFAULT false,
  owner            text NOT NULL REFERENCES person(id),
  content          text NOT NULL,
  kind             text NOT NULL,
  flags            text[] NOT NULL DEFAULT '{}',
  era              text,
  sphere           text,
  visibility       text NOT NULL DEFAULT 'owner',
  exposure         text NOT NULL DEFAULT 'cloud_ok',
  active           boolean NOT NULL DEFAULT true,
  superseded_by    uuid REFERENCES fact(id),
  created_at       timestamptz NOT NULL,
  source_msg       text
);
CREATE INDEX IF NOT EXISTS fact_fts
  ON fact USING gin (to_tsvector('english', content));

-- One row per supporting fact: a relation survives losing one support.
CREATE TABLE IF NOT EXISTS triple (
  id       uuid PRIMARY KEY,
  head     text NOT NULL,
  relation text NOT NULL,
  tail     text NOT NULL,
  fact_id  uuid NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
  UNIQUE (head, relation, tail, fact_id)
);

CREATE TABLE IF NOT EXISTS action_log (
  id        uuid PRIMARY KEY,
  chat_id   bigint NOT NULL,
  tool      text NOT NULL,
  args      jsonb NOT NULL,
  undo_tool text,
  undo_args jsonb,
  done_at   timestamptz NOT NULL,
  undone_at timestamptz
);
CREATE INDEX IF NOT EXISTS action_log_recent
  ON action_log (chat_id, done_at DESC);
