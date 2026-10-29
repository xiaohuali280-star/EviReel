-- E2 re-review: `publish` only decides (ledger commit, attempt-token guarded); the final `project`
-- stage applies that committed decision to the read model. Every attempt of `project` derives the
-- same values from the ledger, so stale or repeated executions cannot disagree with PostgreSQL.
ALTER TABLE job_stages DROP CONSTRAINT job_stages_stage_check;
ALTER TABLE job_stages ADD CONSTRAINT job_stages_stage_check CHECK (stage IN
  ('prepare', 'audio', 'asr', 'transcript_index', 'vision', 'publish', 'project',
   'notes_draft', 'notes_publish', 'export'));
