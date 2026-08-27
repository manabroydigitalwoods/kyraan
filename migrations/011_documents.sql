-- Document memory (owner directive 2026-08-27): captured images with
-- readable text (cards, brochures) and PDFs, transcribed AT INGESTION
-- and retrievable forever. Chunked like episodes; exposure gates which
-- prompts may carry a chunk (local_only never reaches a cloud tier);
-- suppressed_by makes the forget cascade cover documents too.
CREATE TABLE IF NOT EXISTS document (
  id            uuid PRIMARY KEY,
  chat_id       bigint NOT NULL,
  kind          text NOT NULL,               -- photo | pdf
  caption       text NOT NULL DEFAULT '',
  filename      text NOT NULL DEFAULT '',
  text          text NOT NULL,
  flags         text[] NOT NULL DEFAULT '{}',
  exposure      text NOT NULL DEFAULT 'cloud_ok',
  suppressed_by uuid[] NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS document_chunk (
  id          uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  seq         integer NOT NULL,
  text        text NOT NULL,
  embedding   vector(384)
);
CREATE INDEX IF NOT EXISTS document_chunk_ann
  ON document_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunk_fts
  ON document_chunk USING gin (to_tsvector('english', text));
CREATE INDEX IF NOT EXISTS document_chat ON document (chat_id, created_at DESC);
