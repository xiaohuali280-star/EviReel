-- E3 re-review: a removal claim (removed_at: no new lease may name the generation) is separate from the
-- physical cleanup (cleaned_at: its files are confirmed gone). A claim whose cleanup failed or was
-- interrupted stays a sweeper candidate until cleaned_at is set; readers stay refused meanwhile.
-- Rows claimed before this migration are treated as not yet cleaned (deletion is idempotent).
ALTER TABLE retired_generations ADD COLUMN cleaned_at timestamptz;
ALTER TABLE retired_generations ADD COLUMN cleanup_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE retired_generations ADD COLUMN cleanup_error text;
ALTER TABLE retired_generations ADD CONSTRAINT retired_generations_cleaned_after_claim
  CHECK (cleaned_at IS NULL OR removed_at IS NOT NULL);
CREATE INDEX retired_generations_cleanup_pending ON retired_generations(removed_at)
  WHERE removed_at IS NOT NULL AND cleaned_at IS NULL;
