-- Durable object cleanup. A row is written in the same transaction that makes an object
-- unreachable (failed verification, released ownership), so a storage outage or a process
-- exit between the database commit and the storage DELETE cannot leak the object.
CREATE TABLE object_deletions (
  object_key      text PRIMARY KEY,
  reason          text NOT NULL CHECK (reason IN ('upload_failed', 'video_deleted')),
  video_id        text,
  upload_id       text,
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'skipped')),
  attempts        integer NOT NULL DEFAULT 0,
  last_error      text,
  requested_at    timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  done_at         timestamptz,
  CHECK (state = 'pending' OR done_at IS NOT NULL)
);
CREATE INDEX object_deletions_due ON object_deletions(next_attempt_at) WHERE state = 'pending';
