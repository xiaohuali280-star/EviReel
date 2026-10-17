CREATE TABLE IF NOT EXISTS videos (
 video_id TEXT PRIMARY KEY, original_name TEXT NOT NULL, source_path TEXT NOT NULL,
 playback_path TEXT, size_bytes INTEGER NOT NULL, duration REAL NOT NULL,
 created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'uploaded',
 active_generation TEXT, model_fingerprint TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS index_jobs (
 job_id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
 generation TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
 current INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
 attempt INTEGER NOT NULL, logs TEXT NOT NULL DEFAULT '[]', result TEXT,
 error_code TEXT, error TEXT, started_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_video ON index_jobs(video_id, started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active ON index_jobs(video_id)
 WHERE status IN ('queued','running');
