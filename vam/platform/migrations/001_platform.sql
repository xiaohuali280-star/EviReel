-- E1 platform metadata. PostgreSQL owns identities, workspaces, resource ownership,
-- storage quota and resumable upload sessions. Existing per-video metadata
-- (videos, index_jobs, conversations, study) remains in the SQLite registry for E1.

CREATE TABLE users (
  user_id       text PRIMARY KEY CHECK (user_id ~ '^usr_[0-9a-f]{32}$'),
  username      text NOT NULL UNIQUE CHECK (username ~ '^[A-Za-z0-9_.-]{3,64}$'),
  password_hash text NOT NULL,
  disabled      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
  workspace_id   text PRIMARY KEY CHECK (workspace_id ~ '^ws_[0-9a-f]{32}$'),
  name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  quota_bytes    bigint NOT NULL CHECK (quota_bytes >= 0),
  used_bytes     bigint NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
  reserved_bytes bigint NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  workspace_id text NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
  user_id      text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('owner', 'member')),
  is_default   boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
CREATE UNIQUE INDEX memberships_one_default ON memberships(user_id) WHERE is_default;

-- Only the SHA-256 of the bearer token is stored.
CREATE TABLE auth_sessions (
  token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  user_id    text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE INDEX auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE upload_sessions (
  upload_id          text PRIMARY KEY CHECK (upload_id ~ '^up_[0-9a-f]{32}$'),
  workspace_id       text NOT NULL REFERENCES workspaces(workspace_id),
  created_by         text NOT NULL REFERENCES users(user_id),
  video_id           text NOT NULL UNIQUE CHECK (video_id ~ '^v_[0-9a-f]{32}$'),
  original_name      text NOT NULL CHECK (length(original_name) BETWEEN 1 AND 255),
  extension          text NOT NULL,
  content_type       text NOT NULL,
  object_key         text NOT NULL UNIQUE,
  storage_upload_id  text UNIQUE,
  declared_size      bigint NOT NULL CHECK (declared_size > 0),
  part_size          bigint NOT NULL CHECK (part_size > 0),
  part_count         integer NOT NULL CHECK (part_count BETWEEN 1 AND 10000),
  client_fingerprint text NOT NULL,
  state              text NOT NULL CHECK (state IN ('initiated', 'uploading', 'completing', 'verifying',
                                                    'completed', 'failed', 'aborted', 'expired')),
  state_version      integer NOT NULL DEFAULT 0,
  quota_state        text NOT NULL CHECK (quota_state IN ('reserved', 'settled', 'released')),
  expires_at         timestamptz NOT NULL,
  lease_until        timestamptz,
  verify_attempts    integer NOT NULL DEFAULT 0,
  verified_size      bigint,
  verified_sha256    text,
  error_code         text,
  error_message      text,
  result             jsonb,
  job_id             text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  completed_at       timestamptz,
  CHECK (state NOT IN ('uploading', 'completing') OR storage_upload_id IS NOT NULL),
  CHECK (state <> 'completed' OR (verified_size IS NOT NULL AND verified_sha256 IS NOT NULL AND quota_state = 'settled')),
  CHECK (quota_state <> 'reserved' OR state IN ('initiated', 'uploading', 'completing', 'verifying'))
);
CREATE INDEX upload_sessions_workspace_state ON upload_sessions(workspace_id, state);
CREATE INDEX upload_sessions_reconcile ON upload_sessions(state, lease_until)
  WHERE state IN ('initiated', 'completing', 'verifying');
CREATE INDEX upload_sessions_expiry ON upload_sessions(expires_at) WHERE state = 'uploading';

CREATE TABLE upload_parts (
  upload_id   text NOT NULL REFERENCES upload_sessions(upload_id) ON DELETE CASCADE,
  part_number integer NOT NULL CHECK (part_number BETWEEN 1 AND 10000),
  size        bigint NOT NULL CHECK (size > 0),
  md5_b64     text NOT NULL CHECK (length(md5_b64) = 24),
  etag        text,
  state       text NOT NULL CHECK (state IN ('signed', 'uploaded', 'mismatch')),
  signed_at   timestamptz NOT NULL DEFAULT now(),
  uploaded_at timestamptz,
  PRIMARY KEY (upload_id, part_number)
);

-- Single source of truth for which workspace may access a video_id.
CREATE TABLE video_ownership (
  video_id     text PRIMARY KEY CHECK (video_id ~ '^v_[0-9a-f]{32}$'),
  workspace_id text NOT NULL REFERENCES workspaces(workspace_id),
  origin       text NOT NULL CHECK (origin IN ('upload', 'legacy')),
  upload_id    text UNIQUE REFERENCES upload_sessions(upload_id),
  object_key   text,
  size_bytes   bigint NOT NULL CHECK (size_bytes >= 0),
  sha256       text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (origin <> 'upload' OR (upload_id IS NOT NULL AND object_key IS NOT NULL))
);
CREATE INDEX video_ownership_workspace ON video_ownership(workspace_id, created_at DESC);
