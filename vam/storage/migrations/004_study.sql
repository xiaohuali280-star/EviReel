ALTER TABLE videos ADD COLUMN trashed_at REAL;
CREATE TABLE study_jobs (
 job_id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL,
 phase TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
 artifact_id TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(video_id,request_id)
);
CREATE UNIQUE INDEX idx_study_active ON study_jobs(video_id) WHERE status IN ('queued','running');
CREATE TABLE study_artifacts (
 artifact_id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES study_jobs(job_id) ON DELETE CASCADE,
 source_versions TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX idx_study_video ON study_artifacts(video_id,created_at DESC);
