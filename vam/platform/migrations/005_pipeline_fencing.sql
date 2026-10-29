-- E2 acceptance fixes.
-- seq: monotonically increasing per job; every user-visible publication is fenced by it, so a
-- stale attempt of an older job can never overwrite what a newer job already published.
ALTER TABLE jobs ADD COLUMN seq bigserial;
CREATE UNIQUE INDEX jobs_seq ON jobs(seq);
-- Notes batches and exports are pipeline jobs too.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('video', 'transcript', 'notes', 'export'));
-- Exports only read a finished study artifact; they must not block (or be blocked by) indexing.
DROP INDEX jobs_one_active;
CREATE UNIQUE INDEX jobs_one_active ON jobs(video_id) WHERE status IN ('queued', 'running') AND kind <> 'export';
-- Audio extraction is its own stage on the speech branch (a bad audio track must not stop vision).
ALTER TABLE job_stages DROP CONSTRAINT job_stages_stage_check;
ALTER TABLE job_stages ADD CONSTRAINT job_stages_stage_check CHECK (stage IN
  ('prepare', 'audio', 'asr', 'transcript_index', 'vision', 'publish', 'notes_draft', 'notes_publish', 'export'));
-- Unreferenced pipeline products found by the orphan sweep go through the durable deletion queue.
ALTER TABLE object_deletions DROP CONSTRAINT object_deletions_reason_check;
ALTER TABLE object_deletions ADD CONSTRAINT object_deletions_reason_check
  CHECK (reason IN ('upload_failed', 'video_deleted', 'pipeline_orphan'));
