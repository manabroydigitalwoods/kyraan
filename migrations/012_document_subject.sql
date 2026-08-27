-- Person-linked documents (owner: "do we related the doc with person?"
-- -> "go", plus "single doc can relate to multiple person", 2026-08-27).
-- A document ABOUT enrolled persons (Kiaan's vaccination card, a family
-- insurance policy naming Ruma AND Kiaan) carries their person ids;
-- most docs (receipts, brochures) correctly have none. Only ids from
-- the person registry are ever written (validated in code) — the
-- column exists so listing can filter by person and so multi-user
-- document visibility has a handle the day it arrives.
ALTER TABLE document ADD COLUMN IF NOT EXISTS subject_persons text[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS document_subjects_idx
  ON document USING gin (subject_persons);
