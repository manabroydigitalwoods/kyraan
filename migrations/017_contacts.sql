-- Google Contacts sync (governance round 2026-09-01): names are
-- resolvable; phones/emails are LOCAL-ONLY columns — read exclusively
-- by the contacts.find direct-reply path, never into a model prompt.
CREATE TABLE IF NOT EXISTS contact (
    resource   text PRIMARY KEY,          -- Google people/... id
    name       text NOT NULL,
    phones     text[] NOT NULL DEFAULT '{}',
    emails     text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contact_name_idx ON contact (lower(name));
