-- Unified index (owner directive 2026-09-02): Obsidian vault notes join
-- document memory as kind='note' with precise entity linking, and every
-- document row gains the columns the linking model needs.
ALTER TABLE document ADD COLUMN IF NOT EXISTS source_path text NOT NULL DEFAULT '';
ALTER TABLE document ADD COLUMN IF NOT EXISTS entities text[] NOT NULL DEFAULT '{}';
ALTER TABLE document ADD COLUMN IF NOT EXISTS event_date date;
ALTER TABLE document ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS document_source_path_idx ON document (chat_id, source_path)
    WHERE source_path <> '';
CREATE INDEX IF NOT EXISTS document_event_date_idx ON document (chat_id, event_date)
    WHERE event_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS document_entities_idx ON document USING gin (entities);
