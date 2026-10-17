ALTER TABLE videos ADD COLUMN source_sha256 TEXT;
ALTER TABLE videos ADD COLUMN transcript_generation TEXT;
ALTER TABLE videos ADD COLUMN transcript_status TEXT NOT NULL DEFAULT 'not_started';
ALTER TABLE videos ADD COLUMN transcript_error TEXT;
ALTER TABLE index_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'video';
