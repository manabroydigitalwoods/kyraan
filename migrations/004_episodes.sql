-- P3.3a: episodic recall (arch §2.1 column model, complete). The vector
-- dimension is PINNED by the embedder probe (scripts/probe_embedder.py,
-- 2026-08-27: all-minilm, 384-d — lightest AND best margin) and must match
-- store/embed.EMBED_DIM — test_embed pins them together (audit P2:
-- installing a guessed dimension before the probe forces a real
-- migration with rows at stake).
CREATE TABLE IF NOT EXISTS episode (
  id            uuid PRIMARY KEY,
  chat_id       bigint NOT NULL,
  day           date NOT NULL,
  participants  text[] NOT NULL DEFAULT '{}',  -- person ids present
  visibility    text NOT NULL DEFAULT 'owner', -- §4: chat's person + owner
  exposure      text NOT NULL DEFAULT 'cloud_ok',
  flags         text[] NOT NULL DEFAULT '{}',  -- local cheap-tier
                                               -- sensitivity tags: the §3
                                               -- discretion rules must be
                                               -- enforceable on episodes
  fact_refs     uuid[] NOT NULL DEFAULT '{}',  -- facts this evidences
  suppressed_by uuid[] NOT NULL DEFAULT '{}',  -- forget-cascade marks
  text          text NOT NULL,                 -- cloud_text ONLY (twin rule)
  embedding     vector(384) NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS episode_ann
  ON episode USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS episode_flags ON episode USING gin (flags);
CREATE INDEX IF NOT EXISTS episode_fts
  ON episode USING gin (to_tsvector('english', text));
CREATE INDEX IF NOT EXISTS episode_chat_day ON episode (chat_id, day DESC);
