-- P3.2b: the read path ranks with importance/term and expires shorts —
-- columns the index always had but schema v1 didn't mirror. (Episodes
-- and promises take later numbers; the workplan's 002/003 labels were
-- ordinal, not reserved.)
ALTER TABLE fact ADD COLUMN IF NOT EXISTS importance text NOT NULL DEFAULT 'normal';
ALTER TABLE fact ADD COLUMN IF NOT EXISTS term       text NOT NULL DEFAULT 'long';
ALTER TABLE fact ADD COLUMN IF NOT EXISTS target     text;
