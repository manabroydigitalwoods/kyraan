-- Gap audit 2026-08-27: facts whose extraction legitimately yields zero
-- triples (routines, preferences) matched facts_missing_triples forever
-- and were re-sent to the model on every catch-up. Extraction now
-- stamps the fact regardless of yield.
ALTER TABLE fact ADD COLUMN IF NOT EXISTS triples_extracted_at timestamptz;
