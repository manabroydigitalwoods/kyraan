-- Face templates mirrored into the LOCAL Postgres (owner request,
-- 2026-08-27). The files in data/faces/ remain the authority (same
-- doctrine as every store); this table adds pg_dump durability and
-- uniform resync. BIOMETRIC data: this container binds 127.0.0.1 only
-- and the templates still never leave the machine — any future change
-- to the container's exposure must revisit this table first.
CREATE TABLE IF NOT EXISTS face_template (
  id         uuid PRIMARY KEY,
  slug       text NOT NULL,
  name       text NOT NULL,
  embedding  vector(128) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS face_template_slug ON face_template (slug);
