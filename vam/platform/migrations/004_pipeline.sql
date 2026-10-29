-- E2 durable processing pipeline. PostgreSQL is the execution ledger: jobs, stages, attempts
-- (failure ledger), leases/attempt tokens, outbox and verified checkpoints. RabbitMQ only carries
-- "run stage S of job J" hints; every decision is re-checked against these rows.

CREATE TABLE jobs (
  job_id         text PRIMARY KEY CHECK (job_id ~ '^[0-9a-f]{32}$'),
  workspace_id   text REFERENCES workspaces(workspace_id),
  video_id       text NOT NULL CHECK (video_id ~ '^v_[0-9a-f]{32}$'),
  kind           text NOT NULL CHECK (kind IN ('video', 'transcript')),
  request_id     text CHECK (request_id IS NULL OR request_id ~ '^[A-Za-z0-9:_-]{8,80}$'),
  params         jsonb NOT NULL,
  params_digest  text NOT NULL CHECK (params_digest ~ '^[0-9a-f]{64}$'),
  status         text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed')),
  result         jsonb,
  error_code     text,
  error          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  CHECK ((status IN ('succeeded', 'partial', 'failed')) = (finished_at IS NOT NULL))
);
-- Same request id => same job (the digest decides "same request" vs 409 conflict).
CREATE UNIQUE INDEX jobs_request ON jobs((COALESCE(workspace_id, '')), request_id) WHERE request_id IS NOT NULL;
-- At most one unfinished job per video.
CREATE UNIQUE INDEX jobs_one_active ON jobs(video_id) WHERE status IN ('queued', 'running');
CREATE INDEX jobs_video ON jobs(video_id, created_at DESC);

CREATE TABLE job_stages (
  job_id          text NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  stage           text NOT NULL CHECK (stage IN ('prepare', 'asr', 'transcript_index', 'vision', 'publish')),
  -- waiting: dependencies not finished; pending: runnable, a dispatch is (or will be) queued.
  status          text NOT NULL CHECK (status IN ('waiting', 'pending', 'running', 'retry_wait', 'succeeded', 'failed', 'skipped')),
  attempts        integer NOT NULL DEFAULT 0,
  attempt_token   text,
  worker_id       text,
  lease_until     timestamptz,
  heartbeat_at    timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  dispatches      integer NOT NULL DEFAULT 0,
  last_dispatch_at timestamptz,
  output          jsonb,
  error_code      text,
  error           text,
  started_at      timestamptz,
  finished_at     timestamptz,
  PRIMARY KEY (job_id, stage),
  CHECK (status <> 'running' OR (attempt_token IS NOT NULL AND lease_until IS NOT NULL)),
  CHECK ((status IN ('succeeded', 'failed', 'skipped')) = (finished_at IS NOT NULL))
);
CREATE INDEX job_stages_scan ON job_stages(status, next_attempt_at) WHERE status IN ('pending', 'retry_wait');
CREATE INDEX job_stages_leases ON job_stages(lease_until) WHERE status = 'running';

-- One row per claimed attempt: the failure ledger and the audit trail of who ran what.
CREATE TABLE stage_attempts (
  attempt_token text PRIMARY KEY CHECK (attempt_token ~ '^[0-9a-f]{32}$'),
  job_id        text NOT NULL,
  stage         text NOT NULL,
  attempt       integer NOT NULL,
  worker_id     text NOT NULL,
  outcome       text NOT NULL CHECK (outcome IN ('running', 'succeeded', 'retry', 'failed', 'lease_lost')),
  error_code    text,
  error         text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  ended_at      timestamptz,
  FOREIGN KEY (job_id, stage) REFERENCES job_stages(job_id, stage) ON DELETE CASCADE
);
CREATE INDEX stage_attempts_job ON stage_attempts(job_id, stage, attempt);
CREATE INDEX stage_attempts_failures ON stage_attempts(started_at DESC) WHERE outcome IN ('failed', 'retry', 'lease_lost');

-- Transactional outbox. A row is written in the same transaction as the state change that makes a
-- stage runnable; the dispatcher publishes it with broker confirms and only then marks it sent.
CREATE TABLE outbox_events (
  event_id        bigserial PRIMARY KEY,
  dedupe_key      text NOT NULL UNIQUE,
  job_id          text NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  stage           text NOT NULL,
  queue           text NOT NULL,
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
  attempts        integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX outbox_due ON outbox_events(next_attempt_at) WHERE status = 'pending';

-- Verified, reusable stage/batch products. reuse_key covers the source content, model identity,
-- processing parameters and code version; the object is immutable (attempt-keyed) and its sha256
-- was checked after upload, so a row here always points at a complete product.
CREATE TABLE checkpoints (
  reuse_key     text PRIMARY KEY CHECK (reuse_key ~ '^[a-z_]+:[0-9a-f]{64}$'),
  object_key    text NOT NULL,
  sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
  attempt_token text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
