-- RAG (owner directive 2026-08-27): facts gain local all-minilm
-- embeddings so retrieval finds "meditates daily" for "what do I do
-- for stress" — no word overlap needed. NULL = not yet embedded (the
-- embedder was down at mirror time); resync backfills.
ALTER TABLE fact ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS fact_ann
  ON fact USING hnsw (embedding vector_cosine_ops);
