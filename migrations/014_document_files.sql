-- Original file storage (owner: "store the uploaded files in local
-- storage and link them accordingly and therefore we can display the
-- file also", 2026-08-28). The extracted TEXT stays the search/RAG
-- surface; the original bytes (photo jpeg, pdf) now persist under
-- data/documents/<doc_id>.<ext> — owner-only perms like every data
-- dir — and this column links the row to its file. Empty = older doc,
-- no original kept (honestly reported, never faked).
ALTER TABLE document ADD COLUMN IF NOT EXISTS file_path text NOT NULL DEFAULT '';
