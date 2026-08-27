-- Byte-level dedup (owner: "we must check the hash of the file that we
-- can prevent dedup", 2026-08-28). Doc identity is uuid5 of the
-- EXTRACTED TEXT — but the same photo re-sent can OCR slightly
-- differently (model variance) and slip through as a second document.
-- The sha256 of the original bytes catches the re-send regardless.
ALTER TABLE document ADD COLUMN IF NOT EXISTS file_sha256 text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS document_file_hash_idx
  ON document (chat_id, file_sha256) WHERE file_sha256 <> '';
