-- E3: cross-instance coordination and model accounting. All correctness lives in these rows; Redis only
-- carries rate windows, slots, caches and notifications.

-- Read leases replace the per-process lease counters: any API instance reading a published generation
-- (search, playback, export, question answering) holds a row until it releases or the row expires. A
-- crashed instance's leases expire (expires_at) and stop protecting anything after that.
CREATE TABLE read_leases (
  lease_id    uuid PRIMARY KEY,
  video_id    text NOT NULL CHECK (video_id ~ '^v_[0-9a-f]{32}$'),
  generations text[] NOT NULL DEFAULT '{}',
  holder      text NOT NULL,
  purpose     text NOT NULL,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);
CREATE INDEX read_leases_video ON read_leases(video_id, expires_at);

-- Deletion: the tombstone is written first (new leases are refused under the same per-video advisory
-- lock), then existing readers drain or expire, then files and ownership are reclaimed.
CREATE TABLE video_tombstones (
  video_id     text PRIMARY KEY CHECK (video_id ~ '^v_[0-9a-f]{32}$'),
  requested_at timestamptz NOT NULL DEFAULT now(),
  requested_by text,
  reclaimed_at timestamptz,
  attempts     integer NOT NULL DEFAULT 0,
  last_error   text
);

-- A generation replaced by a newer publication is retired, not deleted: its files are removed only after
-- a grace period and once no unexpired read lease names it (older requests keep reading it).
CREATE TABLE retired_generations (
  video_id   text NOT NULL,
  generation text NOT NULL CHECK (generation ~ '^[0-9a-f]{32}$'),
  retired_at timestamptz NOT NULL DEFAULT now(),
  removed_at timestamptz,
  PRIMARY KEY (video_id, generation)
);
CREATE INDEX retired_generations_pending ON retired_generations(retired_at) WHERE removed_at IS NULL;

-- Model usage ledger: one row per provider attempt (retries are separate rows). A reservation is written
-- before the call with the estimate and the price basis used; it is settled with provider-reported usage,
-- marked failed when the provider certainly did not bill (rejected before sending, 4xx), or unknown when
-- the outcome is uncertain (timeout or disconnect after sending). Unknown rows keep counting at their
-- estimate (conservative) until reconciled.
CREATE TABLE model_usage (
  call_id           uuid PRIMARY KEY,
  workspace_id      text NOT NULL,
  provider          text NOT NULL,
  model             text NOT NULL,
  operation         text NOT NULL CHECK (operation IN ('chat', 'embedding', 'rerank', 'study')),
  lane              text NOT NULL CHECK (lane IN ('interactive', 'offline')),
  status            text NOT NULL CHECK (status IN ('reserved', 'settled', 'failed', 'unknown')),
  attempt           integer NOT NULL DEFAULT 1,
  est_input_tokens  integer NOT NULL,
  est_output_tokens integer NOT NULL,
  input_tokens      integer,
  output_tokens     integer,
  est_cost          numeric(16, 8),
  cost              numeric(16, 8),
  price_basis       jsonb NOT NULL,
  usage_source      text CHECK (usage_source IN ('provider', 'estimate')),
  error_code        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  deadline          timestamptz NOT NULL,
  settled_at        timestamptz
);
CREATE INDEX model_usage_open ON model_usage(deadline) WHERE status = 'reserved';
CREATE INDEX model_usage_workspace_day ON model_usage(workspace_id, created_at);

-- Optional per-workspace daily token budget (absent row = settings default). Reservations take a row
-- lock here, so concurrent API instances cannot both admit the last remaining tokens.
CREATE TABLE model_budgets (
  workspace_id      text PRIMARY KEY,
  daily_token_limit bigint NOT NULL CHECK (daily_token_limit >= 0),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Published-file objects (index generations, playback; see vam/publication.py) that left the manifest.
ALTER TABLE object_deletions DROP CONSTRAINT object_deletions_reason_check;
ALTER TABLE object_deletions ADD CONSTRAINT object_deletions_reason_check
  CHECK (reason IN ('upload_failed', 'video_deleted', 'pipeline_orphan', 'superseded'));
