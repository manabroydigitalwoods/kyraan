-- P3.5d: per-person config lives in person COLUMNS, not global config
-- (arch §4): quiet hours honored by can_send_proactively, and a daily
-- model-spend budget enforced by the router per viewer.
ALTER TABLE person ADD COLUMN IF NOT EXISTS dnd_start text;         -- "HH:MM"
ALTER TABLE person ADD COLUMN IF NOT EXISTS dnd_end   text;         -- "HH:MM"
ALTER TABLE person ADD COLUMN IF NOT EXISTS daily_budget_usd numeric;
