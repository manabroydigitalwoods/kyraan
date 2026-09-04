-- Who sent a document (owner 2026-09-04: "it won't create a link with who upload").
ALTER TABLE document ADD COLUMN IF NOT EXISTS uploaded_by text NOT NULL DEFAULT '';
