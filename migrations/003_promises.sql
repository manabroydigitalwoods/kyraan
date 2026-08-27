-- P3.2d: promises to Postgres (arch §2.2) — reminder + agent_task
-- mirror their JSON stores FIELD-FOR-FIELD (when_iso stays the exact
-- ISO string the file holds so parity diffs are byte-comparable;
-- lease/crash-window fields included), plus the cost ledger's flat
-- key→value map.
CREATE TABLE IF NOT EXISTS reminder (
  id               text PRIMARY KEY,
  chat_id          bigint NOT NULL,
  text             text NOT NULL,
  when_iso         text NOT NULL,
  sent             boolean NOT NULL DEFAULT false,
  claimed_at       text NOT NULL DEFAULT '',
  takeover         boolean NOT NULL DEFAULT false,
  repeat           text NOT NULL DEFAULT '',
  interval_minutes integer NOT NULL DEFAULT 0,
  window_start     text NOT NULL DEFAULT '',
  window_end       text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reminder_pending ON reminder (chat_id) WHERE NOT sent;

CREATE TABLE IF NOT EXISTS agent_task (
  id             text PRIMARY KEY,
  chat_id        bigint NOT NULL,
  instruction    text NOT NULL,
  when_iso       text NOT NULL,
  repeat         text NOT NULL DEFAULT '',
  active         boolean NOT NULL DEFAULT true,
  pending_result text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cost_ledger (
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);
