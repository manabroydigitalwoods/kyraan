-- P3.5c: extraction from a non-owner's messages is per-person opt-in
-- (arch §4 first-month rule — default OFF).
ALTER TABLE person ADD COLUMN IF NOT EXISTS extraction_enabled boolean NOT NULL DEFAULT false;
