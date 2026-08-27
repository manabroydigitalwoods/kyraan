-- Name resolution (owner: "How do we make relations between people and
-- his/her face, doc, memories?" -> "resolve it", 2026-08-28). The
-- registry id is the hub every store joins on; aliases make the join
-- deterministic for the names people actually type ("Maan", "Titu",
-- "manab roy") instead of string luck. One resolver, used by document
-- subjects, triple normalization, and face enrollment.
ALTER TABLE person ADD COLUMN IF NOT EXISTS aliases text[] NOT NULL DEFAULT '{}';
