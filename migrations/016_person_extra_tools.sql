-- Per-person capability grants (owner directive 2026-08-28: "owner will
-- have all access but he can give any specific access and we must have
-- roles with specific access"). Roles = stages (the frozen toolset
-- lists); this column is the OWNER'S individual grants on top of a
-- person's stage — "give ruma photo upload" without inventing a new
-- role. Effective access = stage toolset ∪ extra_tools; owner remains
-- all-access by construction.
ALTER TABLE person ADD COLUMN IF NOT EXISTS extra_tools text[] NOT NULL DEFAULT '{}';
