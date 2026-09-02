-- Captures meet the notes they illustrate (owner 2026-09-03: "kiaan has a
-- milestone ... sree krishna dress ... it should create link with all
-- relevant links"). Symmetric: both rows carry the other's id.
ALTER TABLE document ADD COLUMN IF NOT EXISTS related uuid[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS document_related_idx ON document USING gin (related);
